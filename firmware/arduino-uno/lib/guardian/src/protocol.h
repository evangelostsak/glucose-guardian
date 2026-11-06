#ifndef GUARDIAN_PROTOCOL_H
#define GUARDIAN_PROTOCOL_H

#include <stddef.h>
#include <stdint.h>

namespace guardian {

const uint8_t kMaxFrameBytes = 64;
const uint8_t kMaxLineBytes = kMaxFrameBytes - 1;

enum ParseResult : uint8_t {
  kParseOk,
  kParseTooLong,
  kParseEncoding,
  kParseNoChecksum,
  kParseChecksum,
  kParseVersion,
  kParseMalformed,
};

struct StateFrame {
  uint16_t session;
  uint16_t seq;
  char glucose;
  bool fault;
};

struct Report {
  uint16_t boot;
  uint16_t seq;
  uint16_t ackSession;
  uint16_t ackSeq;
  char cause;
  bool buttonStuck;
  uint8_t shortPresses;
  uint8_t longPresses;
};

uint16_t crc16(const char* data, size_t length);
bool seqNewer(uint16_t candidate, uint16_t reference);
ParseResult parseState(const char* line, uint8_t length, StateFrame* out);
uint8_t formatReport(const Report& report, char* out);

class LineReader {
 public:
  LineReader();
  bool push(uint8_t byte);
  const char* line() const { return buffer_; }
  uint8_t length() const { return length_; }
  bool overflowed() const { return overflowed_; }

 private:
  char buffer_[kMaxFrameBytes];
  uint8_t length_;
  bool discarding_;
  bool overflowed_;
  bool complete_;
};

}

#endif
