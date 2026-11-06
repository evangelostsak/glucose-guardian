#include "protocol.h"

namespace guardian {
namespace {

const uint8_t kStateBodyLength = 19;
const uint8_t kChecksumDigits = 4;
const char kHexDigits[] = "0123456789ABCDEF";

bool parseHex(const char* text, uint8_t digits, uint16_t* out) {
  uint16_t value = 0;
  for (uint8_t i = 0; i < digits; ++i) {
    const char c = text[i];
    uint8_t nibble;
    if (c >= '0' && c <= '9') {
      nibble = static_cast<uint8_t>(c - '0');
    } else if (c >= 'A' && c <= 'F') {
      nibble = static_cast<uint8_t>(c - 'A' + 10);
    } else {
      return false;
    }
    value = static_cast<uint16_t>((value << 4) | nibble);
  }
  *out = value;
  return true;
}

char* writeHex(char* out, uint16_t value, uint8_t digits) {
  for (uint8_t i = 0; i < digits; ++i) {
    out[digits - 1 - i] = kHexDigits[value & 0x0F];
    value = static_cast<uint16_t>(value >> 4);
  }
  return out + digits;
}

char* writeText(char* out, const char* text) {
  while (*text != '\0') {
    *out++ = *text++;
  }
  return out;
}

}

uint16_t crc16(const char* data, size_t length) {
  uint16_t crc = 0xFFFF;
  for (size_t i = 0; i < length; ++i) {
    crc = static_cast<uint16_t>(crc ^ (static_cast<uint8_t>(data[i]) << 8));
    for (uint8_t bit = 0; bit < 8; ++bit) {
      crc = (crc & 0x8000) ? static_cast<uint16_t>((crc << 1) ^ 0x1021)
                           : static_cast<uint16_t>(crc << 1);
    }
  }
  return crc;
}

bool seqNewer(uint16_t candidate, uint16_t reference) {
  const uint16_t delta = static_cast<uint16_t>(candidate - reference);
  return delta != 0 && delta < 0x8000;
}

ParseResult parseState(const char* line, uint8_t length, StateFrame* out) {
  if (length > kMaxLineBytes) {
    return kParseTooLong;
  }
  int star = -1;
  for (uint8_t i = 0; i < length; ++i) {
    const uint8_t c = static_cast<uint8_t>(line[i]);
    if (c < 0x20 || c > 0x7E) {
      return kParseEncoding;
    }
    if (c == '*') {
      star = i;
    }
  }
  uint16_t checksum;
  if (star < 0 || length - star - 1 != kChecksumDigits ||
      !parseHex(line + star + 1, kChecksumDigits, &checksum)) {
    return kParseNoChecksum;
  }
  const uint8_t bodyLength = static_cast<uint8_t>(star);
  if (crc16(line, bodyLength) != checksum) {
    return kParseChecksum;
  }
  uint8_t magicLength = 0;
  while (magicLength < bodyLength && line[magicLength] != ' ') {
    ++magicLength;
  }
  const bool versionOne = magicLength == 3 && line[0] == 'G' && line[1] == 'G' && line[2] == '1';
  if (!versionOne) {
    return (magicLength >= 2 && line[0] == 'G' && line[1] == 'G') ? kParseVersion
                                                                   : kParseMalformed;
  }
  StateFrame frame;
  if (bodyLength != kStateBodyLength || line[4] != 'S' || line[5] != ' ' ||
      !parseHex(line + 6, 4, &frame.session) || line[10] != ' ' ||
      !parseHex(line + 11, 4, &frame.seq) || line[15] != ' ' || line[17] != ' ') {
    return kParseMalformed;
  }
  const char glucose = line[16];
  const char fault = line[18];
  if ((glucose != '-' && glucose != 'L' && glucose != 'U' && glucose != 'H') ||
      (fault != '0' && fault != '1')) {
    return kParseMalformed;
  }
  frame.glucose = glucose;
  frame.fault = fault == '1';
  *out = frame;
  return kParseOk;
}

uint8_t formatReport(const Report& report, char* out) {
  char* p = writeText(out, "GG1 R ");
  p = writeHex(p, report.boot, 4);
  *p++ = ' ';
  p = writeHex(p, report.seq, 4);
  *p++ = ' ';
  p = writeHex(p, report.ackSession, 4);
  *p++ = ' ';
  p = writeHex(p, report.ackSeq, 4);
  *p++ = ' ';
  *p++ = report.cause;
  *p++ = ' ';
  p = writeHex(p, report.buttonStuck ? 1 : 0, 2);
  *p++ = ' ';
  p = writeHex(p, report.shortPresses, 2);
  *p++ = ' ';
  p = writeHex(p, report.longPresses, 2);
  const uint16_t checksum = crc16(out, static_cast<size_t>(p - out));
  *p++ = '*';
  p = writeHex(p, checksum, kChecksumDigits);
  *p++ = '\n';
  return static_cast<uint8_t>(p - out);
}

LineReader::LineReader()
    : length_(0), discarding_(false), overflowed_(false), complete_(false) {}

bool LineReader::push(uint8_t byte) {
  if (complete_) {
    length_ = 0;
    overflowed_ = false;
    complete_ = false;
  }
  if (byte == '\n') {
    overflowed_ = discarding_;
    discarding_ = false;
    complete_ = true;
    return true;
  }
  if (discarding_) {
    return false;
  }
  if (length_ == kMaxLineBytes) {
    discarding_ = true;
    length_ = 0;
    return false;
  }
  buffer_[length_++] = static_cast<char>(byte);
  return false;
}

}
