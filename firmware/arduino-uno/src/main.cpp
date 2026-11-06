#include <Arduino.h>

// No GPIO assignments: external drivers and electrical design are unverified.
// Bench protocol: H = heartbeat, N = normal, G = glucose, F = system fault.
// Each command is exactly one character followed by LF. No actuator is driven.
constexpr unsigned long HEARTBEAT_TIMEOUT_MS = 5000;
unsigned long lastHeartbeat = 0;
bool seenHeartbeat = false;
bool glucoseAlarm = false;
bool requestedFault = false;
char command = 0;
bool invalidFrame = false;

void setup() { Serial.begin(115200); }

void loop() {
  while (Serial.available() > 0) {
    const char c = static_cast<char>(Serial.read());
    if (c == '\n') {
      if (!invalidFrame) {
        if (command == 'H') {
          lastHeartbeat = millis();
          seenHeartbeat = true;
          Serial.println(F("HEARTBEAT"));
        } else if (command == 'N') {
          glucoseAlarm = false;
          requestedFault = false;
        } else if (command == 'G') {
          glucoseAlarm = true;
        } else if (command == 'F') {
          requestedFault = true;
        }
      }
      command = 0;
      invalidFrame = false;
    } else if (command == 0 && !invalidFrame) {
      command = c;
    } else {
      invalidFrame = true;
    }
  }
  const bool communicationFault = !seenHeartbeat ||
      static_cast<unsigned long>(millis() - lastHeartbeat) > HEARTBEAT_TIMEOUT_MS;
  // Future reviewed driver layer must distinguish these alarm requests.
  // Button input and bounded acknowledgement are deferred until wiring is selected.
  const bool faultAlarm = requestedFault || communicationFault;
  (void)faultAlarm;
  (void)glucoseAlarm;
}
