#ifndef GUARDIAN_RESET_CAUSE_H
#define GUARDIAN_RESET_CAUSE_H

#include <stdint.h>

namespace guardian {

const uint8_t kPowerOnFlag = 0x01;
const uint8_t kExternalFlag = 0x02;
const uint8_t kBrownOutFlag = 0x04;
const uint8_t kWatchdogFlag = 0x08;
const uint16_t kWatchdogStageMs = 1000;
const uint16_t kWatchdogTimeoutMs = static_cast<uint16_t>(2 * kWatchdogStageMs);
const uint32_t kWatchdogMarker = 0x57444F47UL;

struct ResetMarker {
  uint32_t value;
  uint32_t check;
};

void armWatchdogMarker(volatile ResetMarker& marker);
void clearResetMarker(volatile ResetMarker& marker);
bool takeWatchdogMarker(volatile ResetMarker& marker);
char resetCause(uint8_t mcusr, bool watchdogMarker);

}

#endif
