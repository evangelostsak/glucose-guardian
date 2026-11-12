#include "reset_cause.h"

namespace guardian {

void armWatchdogMarker(volatile ResetMarker& marker) {
  marker.value = kWatchdogMarker;
  marker.check = static_cast<uint32_t>(~kWatchdogMarker);
}

void clearResetMarker(volatile ResetMarker& marker) {
  marker.value = 0;
  marker.check = 0;
}

bool takeWatchdogMarker(volatile ResetMarker& marker) {
  const bool armed = marker.value == kWatchdogMarker &&
                     marker.check == static_cast<uint32_t>(~kWatchdogMarker);
  clearResetMarker(marker);
  return armed;
}

char resetCause(uint8_t mcusr, bool watchdogMarker) {
  if (mcusr & kPowerOnFlag) {
    return 'P';
  }
  if ((mcusr & kWatchdogFlag) || watchdogMarker) {
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
