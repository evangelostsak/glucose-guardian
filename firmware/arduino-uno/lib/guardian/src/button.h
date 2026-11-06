#ifndef GUARDIAN_BUTTON_H
#define GUARDIAN_BUTTON_H

#include <stdint.h>

namespace guardian {

const uint32_t kDebounceMs = 50;
const uint32_t kLongPressMs = 2000;
const uint32_t kStuckMs = 30000;

enum ButtonEvent : uint8_t {
  kButtonNone,
  kButtonShort,
  kButtonLong,
};

class Button {
 public:
  Button();
  ButtonEvent update(bool rawPressed, uint32_t nowMs);
  bool stuck() const { return stuck_; }

 private:
  bool stable_;
  bool candidate_;
  bool stuck_;
  uint32_t candidateSinceMs_;
  uint32_t pressedSinceMs_;
};

}

#endif
