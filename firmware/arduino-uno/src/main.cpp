#include <Arduino.h>
#include <avr/eeprom.h>
#include <avr/wdt.h>

#include "controller.h"
#include "reset_cause.h"

static_assert(guardian::kPowerOnFlag == _BV(PORF), "PORF layout");
static_assert(guardian::kExternalFlag == _BV(EXTRF), "EXTRF layout");
static_assert(guardian::kBrownOutFlag == _BV(BORF), "BORF layout");
static_assert(guardian::kWatchdogFlag == _BV(WDRF), "WDRF layout");
static_assert(guardian::kWatchdogStageMs == 1000, "WDTO_1S");

uint8_t resetFlags __attribute__((section(".noinit")));
volatile guardian::ResetMarker resetMarker __attribute__((section(".noinit")));

void captureResetFlags() __attribute__((naked, used, section(".init3")));
void captureResetFlags() {
  resetFlags = MCUSR;
  MCUSR = 0;
  wdt_disable();
}

ISR(WDT_vect) { guardian::armWatchdogMarker(resetMarker); }

namespace {

uint16_t* const kBootIdAddress = reinterpret_cast<uint16_t*>(0);
const bool kButtonPressed = false;

guardian::Controller controller;
char reply[guardian::kMaxFrameBytes];

#ifdef GUARDIAN_WATCHDOG_TEST
const uint32_t kTestHangAfterMs = 20000;
const uint32_t kTestBannerPeriodMs = 5000;
uint32_t lastBannerMs = 0;
uint16_t testBootId = 0;

void printTestBanner() { Serial.print(F("GGTEST WATCHDOG-HANG FIRMWARE NOT FOR USE\n")); }
#endif

void startWatchdog() {
  wdt_enable(WDTO_1S);
  WDTCSR |= _BV(WDIE);
}

void serviceWatchdog() {
  wdt_reset();
  if ((WDTCSR & _BV(WDIE)) == 0) {
    guardian::clearResetMarker(resetMarker);
    WDTCSR |= _BV(WDIE);
  }
}

}

void setup() {
  const bool watchdogReset = guardian::takeWatchdogMarker(resetMarker);
  startWatchdog();
  const uint16_t bootId = static_cast<uint16_t>(eeprom_read_word(kBootIdAddress) + 1);
  eeprom_update_word(kBootIdAddress, bootId);
  Serial.begin(115200);
  controller.begin(bootId, guardian::resetCause(resetFlags, watchdogReset), millis());
#ifdef GUARDIAN_WATCHDOG_TEST
  testBootId = bootId;
  printTestBanner();
#endif
}

void loop() {
  serviceWatchdog();
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
#ifdef GUARDIAN_WATCHDOG_TEST
  if (now - lastBannerMs >= kTestBannerPeriodMs) {
    lastBannerMs = now;
    printTestBanner();
  }
  if (now >= kTestHangAfterMs) {
    if ((testBootId & 1U) != 0U) {
      cli();
    }
    for (;;) {
      __asm__ __volatile__("");
    }
  }
#endif
}
