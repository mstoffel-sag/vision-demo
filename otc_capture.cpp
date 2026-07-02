// Capture thermal images from an Optris camera (Xi410/Xi400/PI...) over Ethernet.
//
// Uses the native Optris Thermal Camera SDK (libotcsdk). Writes, per frame:
//   <prefix>.png       -> false-color thermal picture (RGB8, via zlib)
//   <prefix>_temp.csv  -> per-pixel temperature in degrees Celsius (with --csv)
//   <prefix>_temp.f32  -> raw little-endian float32 temperatures (with --raw)
//
// Build:
//   g++ -std=c++17 -O2 otc_capture.cpp -o otc_capture -lotcsdk -lz
//
// The SDK is callback driven: we subclass IRImagerClient, run the grabber
// asynchronously, wait for the shutter flag to open (valid thermal data),
// then save the requested number of frames.

#include <atomic>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <ctime>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#include <zlib.h>

#include "otcsdk/Sdk.h"
#include "otcsdk/enumeration/EnumerationManager.h"
#include "otcsdk/IRImager.h"
#include "otcsdk/IRImagerClient.h"
#include "otcsdk/IRImagerFactory.h"
#include "otcsdk/ImageBuilder.h"
#include "otcsdk/Exceptions.h"
#include "otcsdk/common/ThermalFrame.h"
#include "otcsdk/common/FlagState.h"
#include "otcsdk/common/DeviceType.h"

using namespace optris;

// ---------------------------------------------------------------------------
// Minimal PNG writer (8-bit RGB, single IDAT) using zlib.
// ---------------------------------------------------------------------------
static void put_be32(std::vector<unsigned char>& v, uint32_t x)
{
  v.push_back((x >> 24) & 0xff);
  v.push_back((x >> 16) & 0xff);
  v.push_back((x >> 8) & 0xff);
  v.push_back(x & 0xff);
}

static void write_chunk(FILE* f, const char* type, const unsigned char* data, uint32_t len)
{
  unsigned char lenb[4] = {
      (unsigned char)((len >> 24) & 0xff), (unsigned char)((len >> 16) & 0xff),
      (unsigned char)((len >> 8) & 0xff), (unsigned char)(len & 0xff)};
  fwrite(lenb, 1, 4, f);
  fwrite(type, 1, 4, f);
  if (len) fwrite(data, 1, len, f);

  uLong crc = crc32(0L, Z_NULL, 0);
  crc = crc32(crc, (const Bytef*)type, 4);
  if (len) crc = crc32(crc, data, len);
  unsigned char crcb[4] = {
      (unsigned char)((crc >> 24) & 0xff), (unsigned char)((crc >> 16) & 0xff),
      (unsigned char)((crc >> 8) & 0xff), (unsigned char)(crc & 0xff)};
  fwrite(crcb, 1, 4, f);
}

// rgb: tightly packed w*h*3 bytes (padding already stripped).
static bool write_png(const std::string& path, const unsigned char* rgb, int w, int h)
{
  // Build raw scanlines: each row prefixed with a filter-type byte (0 = none).
  std::vector<unsigned char> raw;
  raw.reserve((size_t)h * (1 + (size_t)w * 3));
  for (int y = 0; y < h; ++y)
  {
    raw.push_back(0);
    raw.insert(raw.end(), rgb + (size_t)y * w * 3, rgb + (size_t)(y + 1) * w * 3);
  }

  uLongf comp_len = compressBound(raw.size());
  std::vector<unsigned char> comp(comp_len);
  if (compress2(comp.data(), &comp_len, raw.data(), raw.size(), Z_BEST_SPEED) != Z_OK)
    return false;
  comp.resize(comp_len);

  FILE* f = std::fopen(path.c_str(), "wb");
  if (!f) return false;

  const unsigned char sig[8] = {137, 80, 78, 71, 13, 10, 26, 10};
  fwrite(sig, 1, 8, f);

  std::vector<unsigned char> ihdr;
  put_be32(ihdr, (uint32_t)w);
  put_be32(ihdr, (uint32_t)h);
  ihdr.push_back(8);   // bit depth
  ihdr.push_back(2);   // color type: truecolor RGB
  ihdr.push_back(0);   // compression
  ihdr.push_back(0);   // filter
  ihdr.push_back(0);   // interlace
  write_chunk(f, "IHDR", ihdr.data(), (uint32_t)ihdr.size());
  write_chunk(f, "IDAT", comp.data(), (uint32_t)comp.size());
  write_chunk(f, "IEND", nullptr, 0);

  std::fclose(f);
  return true;
}

// ---------------------------------------------------------------------------
// Camera client: keeps the most recent frame.
// ---------------------------------------------------------------------------
class CaptureClient : public IRImagerClient
{
public:
  explicit CaptureClient(unsigned long serial)
      : _imager{IRImagerFactory::getInstance().create("native")}
  {
    _imager->addClient(this);
    _imager->connect(serial);
  }

  ~CaptureClient() override
  {
    try { _imager->removeClient(this); } catch (...) {}
  }

  void onFrame(const FrameEvent& evt) noexcept override
  {
    std::lock_guard<std::mutex> lk(_mtx);
    _latest = evt;                       // deep copy (safe to keep after return)
    _flag = evt.meta.getFlagState();
    _have = true;
  }

  bool runAsync() { return _imager->runAsync(); }
  void stop() { _imager->stopRunning(); }

  IRImager& imager() { return *_imager; }

  // Returns a copy of the latest frame if the flag is open and data is valid.
  bool takeReadyFrame(FrameEvent& out)
  {
    std::lock_guard<std::mutex> lk(_mtx);
    if (_have && _flag == FlagState::Open && !_latest.thermalFrame.isEmpty())
    {
      out = _latest;
      return true;
    }
    return false;
  }

private:
  std::shared_ptr<IRImager> _imager;
  std::mutex _mtx;
  FrameEvent _latest;
  FlagState _flag = FlagState::Initializing;
  bool _have = false;
};

// ---------------------------------------------------------------------------
static std::string timestamp()
{
  std::time_t t = std::time(nullptr);
  std::tm tm{};
  localtime_r(&t, &tm);
  char buf[32];
  std::strftime(buf, sizeof(buf), "%Y%m%d_%H%M%S", &tm);
  return buf;
}

static void save_frame(const FrameEvent& evt, const std::string& prefix, bool csv, bool raw)
{
  const ThermalFrame& tf = evt.thermalFrame;
  const int w = tf.getWidth();
  const int h = tf.getHeight();

  // 1) False-color picture -> PNG.
  ImageBuilder builder(ColorFormat::RGB, WidthAlignment::OneByte);
  builder.setThermalFrame(tf);
  builder.convertTemperatureToPaletteImage();

  const int stride = builder.getImageStride();          // bytes per row (may be padded)
  const int bytes = builder.getImageSizeInBytes();
  std::vector<unsigned char> img(bytes);
  builder.copyImageDataTo(img.data(), bytes);

  // Strip any row padding down to tight w*3.
  std::vector<unsigned char> rgb((size_t)w * h * 3);
  for (int y = 0; y < h; ++y)
    std::memcpy(rgb.data() + (size_t)y * w * 3, img.data() + (size_t)y * stride, (size_t)w * 3);

  const std::string png = prefix + ".png";
  if (!write_png(png, rgb.data(), w, h))
    std::fprintf(stderr, "WARN: failed to write %s\n", png.c_str());

  // 2) Temperatures in degrees Celsius.
  std::vector<float> temps((size_t)w * h);
  tf.copyTemperaturesTo(temps.data(), (int)temps.size());

  float tmin = temps[0], tmax = temps[0];
  double sum = 0;
  for (float v : temps) { if (v < tmin) tmin = v; if (v > tmax) tmax = v; sum += v; }

  if (raw)
  {
    const std::string fn = prefix + "_temp.f32";
    FILE* f = std::fopen(fn.c_str(), "wb");
    if (f) { std::fwrite(temps.data(), sizeof(float), temps.size(), f); std::fclose(f); }
  }
  if (csv)
  {
    const std::string fn = prefix + "_temp.csv";
    FILE* f = std::fopen(fn.c_str(), "wb");
    if (f)
    {
      for (int y = 0; y < h; ++y)
      {
        for (int x = 0; x < w; ++x)
          std::fprintf(f, x + 1 < w ? "%.2f," : "%.2f", temps[(size_t)y * w + x]);
        std::fputc('\n', f);
      }
      std::fclose(f);
    }
  }

  std::printf("Saved %s  (%dx%d, min %.1f C, max %.1f C, mean %.1f C)\n",
              png.c_str(), w, h, tmin, tmax, sum / temps.size());
  std::fflush(stdout);
}

static void usage(const char* prog)
{
  std::fprintf(stderr,
      "Usage: %s [options]\n"
      "  --serial N         Camera serial number (0 = first detected, default 0)\n"
      "  --network CIDR     Ethernet subnet to scan (default 192.168.0.0/24)\n"
      "  --outdir DIR       Output directory (default ./captures)\n"
      "  --count N          Number of frames to capture (default 1)\n"
      "  --interval-ms MS   Delay between captures (default 1000)\n"
      "  --timeout-s S      Seconds to wait for valid data (default 30)\n"
      "  --csv              Also write per-pixel temperatures as CSV\n"
      "  --raw              Also write raw float32 temperatures (.f32)\n"
      "  --fast-start       Skip startup recalibration for a quicker first frame\n"
      "                     (less accurate initially; good for a fast snapshot)\n",
      prog);
}

int main(int argc, char** argv)
{
  unsigned long serial = 0;
  std::string network = "192.168.0.0/24";
  std::string outdir = "captures";
  int count = 1;
  int interval_ms = 1000;
  int timeout_s = 30;
  bool csv = false, raw = false, fast_start = false;

  for (int i = 1; i < argc; ++i)
  {
    std::string a = argv[i];
    auto next = [&]() { return (i + 1 < argc) ? argv[++i] : ""; };
    if (a == "--serial") serial = std::strtoul(next(), nullptr, 10);
    else if (a == "--network") network = next();
    else if (a == "--outdir") outdir = next();
    else if (a == "--count") count = std::atoi(next());
    else if (a == "--interval-ms") interval_ms = std::atoi(next());
    else if (a == "--timeout-s") timeout_s = std::atoi(next());
    else if (a == "--csv") csv = true;
    else if (a == "--raw") raw = true;
    else if (a == "--fast-start") fast_start = true;
    else if (a == "-h" || a == "--help") { usage(argv[0]); return 0; }
    else { std::fprintf(stderr, "Unknown argument: %s\n", a.c_str()); usage(argv[0]); return 2; }
  }

  Sdk::init(Verbosity::Warning, Verbosity::Off, argv[0]);
  EnumerationManager::getInstance().addEthernetDetector(network);

  CaptureClient* client = nullptr;
  try { client = new CaptureClient(serial); }
  catch (const SDKException& ex) { std::fprintf(stderr, "Connect failed: %s\n", ex.what()); return 1; }

  client->runAsync();
  std::printf("Connected to %s (S/N %lu). Waiting for shutter flag to open...\n",
              toString(client->imager().getDeviceType()).c_str(),
              client->imager().getSerialNumber());
  std::fflush(stdout);

  // Wait until a valid (flag-open) frame is available.
  FrameEvent evt;
  bool skipped = false;
  auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(timeout_s);
  while (!client->takeReadyFrame(evt))
  {
    // Optionally skip the per-run startup recalibration so the first frame is
    // ready sooner. Only has an effect while a startup calibration is active,
    // so retry until it reports success. Trades initial accuracy for speed.
    if (fast_start && !skipped)
      skipped = client->imager().skipStartupCalibration();

    if (std::chrono::steady_clock::now() > deadline)
    {
      std::fprintf(stderr, "Timed out waiting for valid thermal data.\n");
      client->stop();
      delete client;
      return 1;
    }
    std::this_thread::sleep_for(std::chrono::milliseconds(50));
  }

  int rc = 0;
  for (int n = 0; n < count; ++n)
  {
    if (client->takeReadyFrame(evt))
      save_frame(evt, outdir + "/optris_" + timestamp() + "_" + std::to_string(n), csv, raw);
    else
      std::fprintf(stderr, "Frame %d: flag not open, skipped\n", n);

    if (n + 1 < count)
      std::this_thread::sleep_for(std::chrono::milliseconds(interval_ms));
  }

  client->stop();
  delete client;
  std::printf("Done.\n");
  return rc;
}
