/**
 * relay-auto-open — release a Cumulocity-closed relay after a fixed delay.
 *
 * Cumulocity delivers relay operations as JSON on c8y/devicecontrol/notifications:
 *
 *   {"id":"1800380","deviceId":"201802315","status":"PENDING",
 *    "c8y_Relay":{"relayState":"CLOSED"}, ...}
 *
 * onMessage arms a deadline when it sees CLOSED; onInterval emits the matching
 * OPEN operation once that deadline passes. It goes out on the internal request
 * topic, because the runtime refuses to let a flow publish to a topic it also
 * subscribes to; emit.toml forwards it to c8y/devicecontrol/notifications, where
 * the mapper's on_fragment handler drives the output exactly as it would for an
 * operator-issued OPEN.
 *
 * Design notes:
 *
 * - FIRST CLOSE WINS. A second CLOSED while already armed does not push the
 *   deadline out. The point of the flow is a bounded closed-time, so a repeated
 *   CLOSED — a retry, a re-delivery, an operator clicking twice — must not be
 *   able to extend that indefinitely. Re-closing after an auto-open starts a
 *   fresh episode normally.
 *
 * - ANY OPEN DISARMS: the one this flow emits (it arrives straight back on the
 *   subscribed topic), and one an operator sends early. That echo is what makes
 *   the flow safe against feedback — OPEN never arms anything, so it cannot
 *   drive itself in a loop.
 *
 * - THE OUTGOING OPERATION IS REBUILT FROM THE TRIGGER, copying externalSource
 *   (and deviceId/agentId) off the CLOSED that armed it. externalSource is not
 *   decoration: thin-edge's C8yOperation requires `externalSource` and `id` and
 *   drops the message without them — every other field is optional extras.
 *   Copying it also keeps the flow portable, since it never has to be told which
 *   device it runs on.
 *
 * - THE ID IS SYNTHETIC (a millisecond timestamp: numeric, and unique). No such
 *   operation exists in Cumulocity. thin-edge reports operation results with
 *   name-addressed SmartREST (501/503 c8y_Relay) rather than by id, so the relay
 *   still opens; expect Cumulocity to log the status update as unmatched when no
 *   c8y_Relay operation is pending. Uniqueness matters because thin-edge
 *   de-duplicates operations that arrive in quick succession by id.
 *
 * - STATE IS IN MEMORY (context.flow, which the runtime does not persist across
 *   mapper restarts). A restart while a relay is closed loses the deadline and
 *   leaves the relay closed. See README.md — this is called out rather than
 *   papered over.
 */

const decoder = new TextDecoder();

const DEFAULT_DELAY_MINUTES = 5;
const DEFAULT_FRAGMENT = "c8y_Relay";
const DEFAULT_REQUEST_TOPIC = "relay-auto-open/open";
const ARMED = "armed";

/** The operation JSON, or null when this message is not one. */
function parseOperation(message) {
    try {
        return JSON.parse(decoder.decode(message.payload));
    } catch (_) {
        return null;
    }
}

/** "CLOSED" / "OPEN" for a relay operation, or null when this is not one. */
function relayStateOf(operation, fragment) {
    const value = operation ? operation[fragment] : null;
    const state = value ? value.relayState : null;
    return typeof state === "string" ? state.toUpperCase() : null;
}

function delayMillis(config) {
    const minutes = Number(config.delay_minutes);
    const valid = Number.isFinite(minutes) && minutes > 0 ? minutes : DEFAULT_DELAY_MINUTES;
    return Math.round(valid * 60000);
}

export function onMessage(message, context) {
    const config = context.config || {};
    const fragment = config.relay_fragment || DEFAULT_FRAGMENT;

    const operation = parseOperation(message);
    const state = relayStateOf(operation, fragment);
    if (state === null) {
        return [];                                  // not a relay operation
    }

    if (state !== "CLOSED") {
        context.flow.set(ARMED, null);              // an OPEN went past
        return [];
    }

    if (context.flow.get(ARMED)) {
        return [];                                  // first close wins
    }

    if (!operation.externalSource) {
        // thin-edge's own C8yOperation requires externalSource, so the mapper
        // dropped this operation too and the relay never closed. Nothing to
        // release — and arming here would emit an OPEN the mapper would reject
        // for exactly the same reason.
        return [];
    }

    context.flow.set(ARMED, {
        open_at: message.time.getTime() + delayMillis(config),
        request_topic: config.request_topic || DEFAULT_REQUEST_TOPIC,
        fragment: fragment,
        deviceId: operation.deviceId,
        agentId: operation.agentId,
        externalSource: operation.externalSource,
    });
    return [];
}

export function onInterval(time, context) {
    const armed = context.flow.get(ARMED);
    if (!armed || time.getTime() < armed.open_at) {
        return [];
    }
    context.flow.set(ARMED, null);                  // disarm first, never re-fire

    const operation = {
        id: String(time.getTime()),
        status: "PENDING",
        description: "Auto-open relay (relay-auto-open flow)",
        creationTime: time.toISOString(),
    };
    operation[armed.fragment] = { relayState: "OPEN" };
    operation.externalSource = armed.externalSource;   // required by the mapper
    if (armed.deviceId !== undefined) operation.deviceId = armed.deviceId;
    if (armed.agentId !== undefined) operation.agentId = armed.agentId;

    return [{ topic: armed.request_topic, payload: JSON.stringify(operation) }];
}
