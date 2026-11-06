#ifndef GUARDIAN_CONTROLLER_H
#define GUARDIAN_CONTROLLER_H

#include <stdint.h>

#include "button.h"
#include "pi_link.h"
#include "protocol.h"

namespace guardian {

struct Demand {
  char glucose;
  bool fault;
  bool linkLost;
};

class Controller {
 public:
  Controller();
  void begin(uint16_t bootId, char resetCause, uint32_t nowMs);
  uint8_t receive(uint8_t byte, uint32_t nowMs, char* reply);
  void button(bool rawPressed, uint32_t nowMs);
  Demand demand(uint32_t nowMs);
  uint16_t rejectedFrames() const { return rejected_; }

 private:
  LineReader reader_;
  PiLink link_;
  Button button_;
  uint16_t bootId_;
  uint16_t seq_;
  uint16_t rejected_;
  char cause_;
  char glucose_;
  bool fault_;
  uint8_t shortPresses_;
  uint8_t longPresses_;
};

}

#endif
