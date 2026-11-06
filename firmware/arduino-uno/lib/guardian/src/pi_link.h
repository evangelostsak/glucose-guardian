#ifndef GUARDIAN_PI_LINK_H
#define GUARDIAN_PI_LINK_H

#include <stdint.h>

#include "protocol.h"

namespace guardian {

const uint32_t kHeartbeatTimeoutMs = 5000;
const uint32_t kStartupGraceMs = 10000;
const uint32_t kLocalSilenceMs = 60000;

class PiLink {
 public:
  explicit PiLink(uint32_t bootMs);
  bool accept(const StateFrame& frame, uint32_t nowMs);
  bool lost(uint32_t nowMs);
  bool silence(uint32_t nowMs);
  bool silenced(uint32_t nowMs);

 private:
  uint32_t bootMs_;
  uint32_t lastMs_;
  uint32_t silenceStartMs_;
  uint16_t session_;
  uint16_t seq_;
  bool seen_;
  bool lost_;
  bool silenceActive_;
};

}

#endif
