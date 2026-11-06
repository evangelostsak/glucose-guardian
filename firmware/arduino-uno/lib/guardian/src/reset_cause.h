#ifndef GUARDIAN_RESET_CAUSE_H
#define GUARDIAN_RESET_CAUSE_H

#include <stdint.h>

namespace guardian {

const uint8_t kPowerOnFlag = 0x01;
const uint8_t kExternalFlag = 0x02;
const uint8_t kBrownOutFlag = 0x04;
const uint8_t kWatchdogFlag = 0x08;
const uint16_t kWatchdogTimeoutMs = 2000;

char resetCause(uint8_t mcusr);

}

#endif
