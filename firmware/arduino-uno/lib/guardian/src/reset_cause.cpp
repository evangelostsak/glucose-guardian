#include "reset_cause.h"

namespace guardian {

char resetCause(uint8_t mcusr) {
  if (mcusr & kPowerOnFlag) {
    return 'P';
  }
  if (mcusr & kWatchdogFlag) {
    return 'W';
  }
  if (mcusr & kBrownOutFlag) {
    return 'B';
  }
  if (mcusr & kExternalFlag) {
    return 'E';
  }
  return 'U';
}

}
