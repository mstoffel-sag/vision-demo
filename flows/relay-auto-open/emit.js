/**
 * relay-auto-open (2/2) — forward a prepared relay operation to Cumulocity's
 * operation topic.
 *
 * The payload is already a complete c8y operation built by main.js, so this
 * step only re-addresses it: keeping the construction in one place means there
 * is a single definition of what gets sent.
 *
 * The topic is set here rather than through `[output.mqtt] topic` in emit.toml,
 * because that setting does not act as a default — a returned message with no
 * `topic` is rejected with "Message is missing the 'topic' property".
 */
const OPERATION_TOPIC = "c8y/devicecontrol/notifications";

export function onMessage(message, context) {
    return [{ topic: OPERATION_TOPIC, payload: message.payload }];
}
