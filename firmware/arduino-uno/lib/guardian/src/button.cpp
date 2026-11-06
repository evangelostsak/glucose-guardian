#include "button.h"

namespace guardian {

Button::Button()
    : stable_(false), candidate_(false), stuck_(false), candidateSinceMs_(0), pressedSinceMs_(0) {}

ButtonEvent Button::update(bool rawPressed, uint32_t nowMs) {
  if (rawPressed != candidate_) {
    candidate_ = rawPressed;
    candidateSinceMs_ = nowMs;
  }
  ButtonEvent event = kButtonNone;
  if (candidate_ != stable_ && nowMs - candidateSinceMs_ >= kDebounceMs) {
    stable_ = candidate_;
    if (stable_) {
      pressedSinceMs_ = candidateSinceMs_;
    } else {
      if (!stuck_) {
        event = candidateSinceMs_ - pressedSinceMs_ >= kLongPressMs ? kButtonLong : kButtonShort;
      }
      stuck_ = false;
    }
  }
  if (stable_ && !stuck_ && nowMs - pressedSinceMs_ >= kStuckMs) {
    stuck_ = true;
  }
  return event;
}

}
