#include "pi_link.h"

namespace guardian {

PiLink::PiLink(uint32_t bootMs)
    : bootMs_(bootMs),
      lastMs_(bootMs),
      silenceStartMs_(bootMs),
      session_(0),
      seq_(0),
      seen_(false),
      lost_(false),
      silenceActive_(false) {}

bool PiLink::accept(const StateFrame& frame, uint32_t nowMs) {
  if (seen_ && frame.session == session_ && !seqNewer(frame.seq, seq_)) {
    return false;
  }
  session_ = frame.session;
  seq_ = frame.seq;
  seen_ = true;
  lastMs_ = nowMs;
  lost_ = false;
  silenceActive_ = false;
  return true;
}

bool PiLink::lost(uint32_t nowMs) {
  if (!lost_) {
    lost_ = seen_ ? nowMs - lastMs_ > kHeartbeatTimeoutMs : nowMs - bootMs_ >= kStartupGraceMs;
  }
  return lost_;
}

bool PiLink::silence(uint32_t nowMs) {
  if (!lost(nowMs) || silenced(nowMs)) {
    return false;
  }
  silenceActive_ = true;
  silenceStartMs_ = nowMs;
  return true;
}

bool PiLink::silenced(uint32_t nowMs) {
  if (silenceActive_ && nowMs - silenceStartMs_ >= kLocalSilenceMs) {
    silenceActive_ = false;
  }
  return silenceActive_;
}

}
