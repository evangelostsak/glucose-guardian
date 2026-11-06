#include <Arduino.h>
#include <avr/eeprom.h>
#include <avr/wdt.h>

#include "controller.h"
#include "reset_cause.h"

static_assert(guardian::kPowerOnFlag == _BV(PORF), "PORF layout");
static_assert(guardian::kExternalFlag == _BV(EXTRF), "EXTRF layout");
static_assert(guardian::kBrownOutFlag == _BV(BORF), "BORF layout");
static_assert(guardian::kWatchdogFlag == _BV(WDRF), "WDRF layout");
static_assert(guardian::kWatchdogTimeoutMs == 2000, "WDTO_2S");

uint8_t resetFlags __attribute__((section(".noinit")));

void captureResetFlags() __attribute__((naked, used, section(".init3")));
void captureResetFlags() {
  resetFlags = MCUSR;
  MCUSR = 0;
  wdt_disable();
}

namespace {

uint16_t* const kBootIdAddress = reinterpret_cast<uint16_t*>(0);
const bool kButtonPressed = false;

guardian::Controller controller;
char reply[guardian::kMaxFrameBytes];

}

void setup() {
  wdt_enable(WDTO_2S);
  const uint16_t bootId = static_cast<uint16_t>(eeprom_read_word(kBootIdAddress) + 1);
  eeprom_update_word(kBootIdAddress, bootId);
  Serial.begin(115200);
  controller.begin(bootId, guardian::resetCause(resetFlags), millis());
}

void loop() {
  wdt_reset();
  const uint32_t now = millis();
  while (Serial.available() > 0) {
    const uint8_t length = controller.receive(static_cast<uint8_t>(Serial.read()), now, reply);
    if (length > 0) {
      Serial.write(reply, length);
    }
  }
  controller.button(kButtonPressed, now);
  const guardian::Demand demand = controller.demand(now);
  (void)demand;
}
