#include "controller.h"

namespace guardian {

Controller::Controller()
    : link_(0),
      bootId_(0),
      seq_(0),
      rejected_(0),
      cause_('U'),
      glucose_('-'),
      fault_(false),
      shortPresses_(0),
      longPresses_(0) {}

void Controller::begin(uint16_t bootId, char resetCause, uint32_t nowMs) {
  link_ = PiLink(nowMs);
  bootId_ = bootId;
  cause_ = resetCause;
}

uint8_t Controller::receive(uint8_t byte, uint32_t nowMs, char* reply) {
  if (!reader_.push(byte)) {
    return 0;
  }
  StateFrame frame;
  if (reader_.overflowed() ||
      parseState(reader_.line(), reader_.length(), &frame) != kParseOk ||
      !link_.accept(frame, nowMs)) {
    ++rejected_;
    return 0;
  }
  glucose_ = frame.glucose;
  fault_ = frame.fault;
  ++seq_;
  const Report report = {bootId_,     seq_,         frame.session, frame.seq,
                         cause_,      button_.stuck(), shortPresses_, longPresses_};
  return formatReport(report, reply);
}

void Controller::button(bool rawPressed, uint32_t nowMs) {
  const ButtonEvent event = button_.update(rawPressed, nowMs);
  const bool lost = link_.lost(nowMs);
  if (event == kButtonShort && !lost) {
    ++shortPresses_;
  } else if (event == kButtonLong) {
    if (lost) {
      link_.silence(nowMs);
    } else {
      ++longPresses_;
    }
  }
}

Demand Controller::demand(uint32_t nowMs) {
  if (link_.lost(nowMs)) {
    const Demand demand = {'-', !link_.silenced(nowMs), true};
    return demand;
  }
  const Demand demand = {glucose_, fault_, false};
  return demand;
}

}
