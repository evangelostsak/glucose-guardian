#include <string.h>
#include <unity.h>

#include "button.h"
#include "controller.h"
#include "pi_link.h"
#include "protocol.h"
#include "reset_cause.h"

using namespace guardian;

namespace {

const char kStateVector[] = "GG1 S 7F3A 0001 U 1*03EE";
const char kQuietVector[] = "GG1 S 7F3A FFFF - 0*C5CA";

void writeHex4(char* out, uint16_t value) {
  const char digits[] = "0123456789ABCDEF";
  for (int i = 3; i >= 0; --i) {
    out[i] = digits[value & 0x0F];
    value = static_cast<uint16_t>(value >> 4);
  }
}

uint8_t sign(const char* body, char* out) {
  const size_t length = strlen(body);
  memcpy(out, body, length);
  out[length] = '*';
  writeHex4(out + length + 1, crc16(body, length));
  return static_cast<uint8_t>(length + 5);
}

ParseResult parse(const char* line, StateFrame* frame) {
  return parseState(line, static_cast<uint8_t>(strlen(line)), frame);
}

StateFrame frame(uint16_t session, uint16_t seq) {
  const StateFrame result = {session, seq, 'U', true};
  return result;
}

void expectReport(const char* reply, uint8_t length, const char* body) {
  const size_t bodyLength = strlen(body);
  TEST_ASSERT_EQUAL_UINT8(bodyLength + 6, length);
  TEST_ASSERT_EQUAL_MEMORY(body, reply, bodyLength);
  char checksum[4];
  writeHex4(checksum, crc16(body, bodyLength));
  TEST_ASSERT_EQUAL_CHAR('*', reply[bodyLength]);
  TEST_ASSERT_EQUAL_MEMORY(checksum, reply + bodyLength + 1, 4);
  TEST_ASSERT_EQUAL_CHAR('\n', reply[length - 1]);
}

uint8_t feed(Controller& controller, const char* line, uint32_t nowMs, char* reply) {
  uint8_t length = 0;
  for (const char* c = line; *c != '\0'; ++c) {
    length = controller.receive(static_cast<uint8_t>(*c), nowMs, reply);
    TEST_ASSERT_EQUAL_UINT8(0, length);
  }
  return controller.receive('\n', nowMs, reply);
}

uint8_t feedState(Controller& controller, uint16_t seq, uint32_t nowMs, char* reply) {
  char body[32];
  char line[40];
  memcpy(body, "GG1 S 7F3A ", 11);
  writeHex4(body + 11, seq);
  memcpy(body + 15, " U 1", 5);
  const uint8_t length = sign(body, line);
  line[length] = '\0';
  return feed(controller, line, nowMs, reply);
}

void press(Controller& controller, uint32_t startMs, uint32_t heldMs) {
  controller.button(true, startMs);
  controller.button(true, startMs + kDebounceMs);
  controller.button(true, startMs + heldMs);
  controller.button(false, startMs + heldMs);
  controller.button(false, startMs + heldMs + kDebounceMs);
}

ButtonEvent hold(Button& button, uint32_t startMs, uint32_t heldMs) {
  ButtonEvent event = kButtonNone;
  const uint32_t steps[] = {startMs, startMs + kDebounceMs, startMs + heldMs};
  for (uint32_t t : steps) {
    event = button.update(true, t);
    TEST_ASSERT_EQUAL(kButtonNone, event);
  }
  TEST_ASSERT_EQUAL(kButtonNone, button.update(false, startMs + heldMs));
  return button.update(false, startMs + heldMs + kDebounceMs);
}

}

void setUp() {}
void tearDown() {}

void test_crc_check_value() { TEST_ASSERT_EQUAL_HEX16(0x29B1, crc16("123456789", 9)); }

void test_parse_shared_state_vectors() {
  StateFrame f;
  TEST_ASSERT_EQUAL(kParseOk, parse(kStateVector, &f));
  TEST_ASSERT_EQUAL_HEX16(0x7F3A, f.session);
  TEST_ASSERT_EQUAL_HEX16(0x0001, f.seq);
  TEST_ASSERT_EQUAL_CHAR('U', f.glucose);
  TEST_ASSERT_TRUE(f.fault);
  TEST_ASSERT_EQUAL(kParseOk, parse(kQuietVector, &f));
  TEST_ASSERT_EQUAL_HEX16(0xFFFF, f.seq);
  TEST_ASSERT_EQUAL_CHAR('-', f.glucose);
  TEST_ASSERT_FALSE(f.fault);
}

void test_format_shared_report_vectors() {
  char out[kMaxFrameBytes];
  const Report report = {0x00A1, 0x0002, 0x7F3A, 0x0001, 'E', false, 3, 1};
  uint8_t length = formatReport(report, out);
  TEST_ASSERT_EQUAL_STRING_LEN("GG1 R 00A1 0002 7F3A 0001 E 00 03 01*505C\n", out, length);
  const Report maximum = {0xFFFF, 0xFFFF, 0x7F3A, 0xFFFF, 'W', true, 0xFF, 0x00};
  length = formatReport(maximum, out);
  TEST_ASSERT_EQUAL_STRING_LEN("GG1 R FFFF FFFF 7F3A FFFF W 01 FF 00*047D\n", out, length);
  TEST_ASSERT_TRUE(length <= kMaxFrameBytes);
}

void test_parse_rejections() {
  StateFrame f;
  char line[kMaxFrameBytes + 2];
  TEST_ASSERT_EQUAL(kParseVersion, parse("GG2 S 7F3A 0001 U 1*32C8", &f));
  TEST_ASSERT_EQUAL(kParseMalformed, parse("GG1 S 7f3a 0001 U 1*5443", &f));
  TEST_ASSERT_EQUAL(kParseMalformed, parse("GG1 S 7F3A 0001 X 1*41BF", &f));
  TEST_ASSERT_EQUAL(kParseChecksum, parse("GG1 S 7F3A 0001 U 1*03EF", &f));
  TEST_ASSERT_EQUAL(kParseChecksum, parse("GG1 S 7F3A 0002 U 1*03EE", &f));
  TEST_ASSERT_EQUAL(kParseNoChecksum, parse("GG1 S 7F3A 0001 U 1*03ee", &f));
  TEST_ASSERT_EQUAL(kParseNoChecksum, parse("GG1 S 7F3A 0001 U 1", &f));
  TEST_ASSERT_EQUAL(kParseNoChecksum, parse("GG1 S 7F3A 0001 U 1*03EE0", &f));
  TEST_ASSERT_EQUAL(kParseEncoding, parse("GG1 S 7F3A 0001 U 1*03EE\r", &f));
  TEST_ASSERT_EQUAL(kParseMalformed, parse("GG1 R 00A1 0002 7F3A 0001 E 00 03 01*505C", &f));
  uint8_t length = sign("GG1", line);
  TEST_ASSERT_EQUAL(kParseMalformed, parseState(line, length, &f));
  length = sign("HELLO", line);
  TEST_ASSERT_EQUAL(kParseMalformed, parseState(line, length, &f));
  length = sign("GG12 S 7F3A 0001 U 1", line);
  TEST_ASSERT_EQUAL(kParseVersion, parseState(line, length, &f));
  memset(line, 'A', sizeof(line));
  TEST_ASSERT_EQUAL(kParseTooLong, parseState(line, kMaxLineBytes + 1, &f));
  line[0] = '\0';
  TEST_ASSERT_EQUAL(kParseEncoding, parseState(line, 1, &f));
}

void test_seq_newer_wraps() {
  TEST_ASSERT_TRUE(seqNewer(1, 0));
  TEST_ASSERT_FALSE(seqNewer(0, 0));
  TEST_ASSERT_FALSE(seqNewer(0, 1));
  TEST_ASSERT_TRUE(seqNewer(0, 0xFFFF));
  TEST_ASSERT_TRUE(seqNewer(0x7FFF, 0));
  TEST_ASSERT_FALSE(seqNewer(0x8000, 0));
}

void test_line_reader_bounds_and_recovers() {
  LineReader reader;
  for (uint8_t i = 0; i < kMaxLineBytes; ++i) {
    TEST_ASSERT_FALSE(reader.push('A'));
  }
  TEST_ASSERT_TRUE(reader.push('\n'));
  TEST_ASSERT_FALSE(reader.overflowed());
  TEST_ASSERT_EQUAL_UINT8(kMaxLineBytes, reader.length());
  for (int i = 0; i < 1000; ++i) {
    TEST_ASSERT_FALSE(reader.push('B'));
  }
  TEST_ASSERT_TRUE(reader.push('\n'));
  TEST_ASSERT_TRUE(reader.overflowed());
  TEST_ASSERT_FALSE(reader.push('C'));
  TEST_ASSERT_TRUE(reader.push('\n'));
  TEST_ASSERT_FALSE(reader.overflowed());
  TEST_ASSERT_EQUAL_UINT8(1, reader.length());
  TEST_ASSERT_EQUAL_CHAR('C', reader.line()[0]);
}

void test_startup_grace() {
  PiLink link(1000);
  TEST_ASSERT_FALSE(link.lost(10999));
  TEST_ASSERT_TRUE(link.lost(11000));
}

void test_heartbeat_timeout_boundary() {
  PiLink link(0);
  TEST_ASSERT_TRUE(link.accept(frame(0x7F3A, 1), 3000));
  TEST_ASSERT_FALSE(link.lost(8000));
  TEST_ASSERT_TRUE(link.lost(8001));
  TEST_ASSERT_TRUE(link.accept(frame(0x7F3A, 2), 9000));
  TEST_ASSERT_FALSE(link.lost(9000));
}

void test_timeout_latches_across_millis_wrap() {
  PiLink link(0);
  link.accept(frame(0x7F3A, 1), 1000);
  TEST_ASSERT_TRUE(link.lost(6001));
  TEST_ASSERT_TRUE(link.lost(4000));
}

void test_duplicates_and_replays_never_refresh_liveness() {
  PiLink link(0);
  TEST_ASSERT_TRUE(link.accept(frame(0x7F3A, 5), 1000));
  TEST_ASSERT_FALSE(link.accept(frame(0x7F3A, 5), 5900));
  TEST_ASSERT_FALSE(link.accept(frame(0x7F3A, 4), 5950));
  TEST_ASSERT_TRUE(link.lost(6001));
}

void test_sequence_wrap_and_pi_restart_resync() {
  PiLink link(0);
  TEST_ASSERT_TRUE(link.accept(frame(0x7F3A, 0xFFFF), 100));
  TEST_ASSERT_TRUE(link.accept(frame(0x7F3A, 0x0000), 200));
  TEST_ASSERT_TRUE(link.accept(frame(0x1234, 0x0000), 300));
  TEST_ASSERT_FALSE(link.accept(frame(0x1234, 0x0000), 400));
}

void test_local_silence_is_bounded_not_extendable_and_rearms() {
  PiLink link(0);
  TEST_ASSERT_FALSE(link.silence(5000));
  TEST_ASSERT_TRUE(link.silence(20000));
  TEST_ASSERT_FALSE(link.silence(50000));
  TEST_ASSERT_TRUE(link.silenced(79999));
  TEST_ASSERT_FALSE(link.silenced(80000));
  TEST_ASSERT_TRUE(link.lost(80000));
  TEST_ASSERT_TRUE(link.silence(90000));
  link.accept(frame(0x7F3A, 1), 91000);
  TEST_ASSERT_FALSE(link.silenced(91000));
  TEST_ASSERT_FALSE(link.silence(91000));
}

void test_button_debounce_ignores_glitches() {
  Button button;
  TEST_ASSERT_EQUAL(kButtonNone, button.update(true, 0));
  TEST_ASSERT_EQUAL(kButtonNone, button.update(true, 49));
  TEST_ASSERT_EQUAL(kButtonNone, button.update(false, 49));
  TEST_ASSERT_EQUAL(kButtonNone, button.update(false, 500));
  TEST_ASSERT_EQUAL(kButtonNone, button.update(true, 600));
  TEST_ASSERT_EQUAL(kButtonNone, button.update(false, 620));
  TEST_ASSERT_EQUAL(kButtonNone, button.update(false, 2000));
}

void test_button_short_and_long_boundaries() {
  Button button;
  TEST_ASSERT_EQUAL(kButtonShort, hold(button, 0, 300));
  TEST_ASSERT_EQUAL(kButtonShort, hold(button, 1000, kLongPressMs - 1));
  TEST_ASSERT_EQUAL(kButtonLong, hold(button, 5000, kLongPressMs));
  TEST_ASSERT_EQUAL(kButtonLong, hold(button, 10000, kStuckMs - 1));
}

void test_stuck_button_reports_and_never_acknowledges() {
  Button button;
  button.update(true, 0);
  button.update(true, kDebounceMs);
  TEST_ASSERT_FALSE(button.stuck());
  button.update(true, kStuckMs - 1);
  TEST_ASSERT_FALSE(button.stuck());
  button.update(true, kStuckMs);
  TEST_ASSERT_TRUE(button.stuck());
  TEST_ASSERT_EQUAL(kButtonNone, button.update(false, kStuckMs + 100));
  TEST_ASSERT_EQUAL(kButtonNone, button.update(false, kStuckMs + 100 + kDebounceMs));
  TEST_ASSERT_FALSE(button.stuck());
  TEST_ASSERT_EQUAL(kButtonShort, hold(button, kStuckMs + 1000, 200));
}

void test_reset_cause_priority() {
  TEST_ASSERT_EQUAL_CHAR('P', resetCause(kPowerOnFlag | kBrownOutFlag, false));
  TEST_ASSERT_EQUAL_CHAR('W', resetCause(kWatchdogFlag | kExternalFlag, false));
  TEST_ASSERT_EQUAL_CHAR('B', resetCause(kBrownOutFlag | kExternalFlag, false));
  TEST_ASSERT_EQUAL_CHAR('E', resetCause(kExternalFlag, false));
  TEST_ASSERT_EQUAL_CHAR('U', resetCause(0, false));
  TEST_ASSERT_EQUAL_UINT16(1000, kWatchdogStageMs);
  TEST_ASSERT_EQUAL_UINT16(2000, kWatchdogTimeoutMs);
}

void test_cleared_flags_report_watchdog_only_from_the_marker() {
  TEST_ASSERT_EQUAL_CHAR('W', resetCause(0, true));
  TEST_ASSERT_EQUAL_CHAR('U', resetCause(0, false));
  TEST_ASSERT_EQUAL_CHAR('P', resetCause(kPowerOnFlag, true));
  TEST_ASSERT_EQUAL_CHAR('W', resetCause(kBrownOutFlag | kExternalFlag, true));
}

void test_watchdog_marker_is_reported_once() {
  ResetMarker marker = {0, 0};
  armWatchdogMarker(marker);
  TEST_ASSERT_TRUE(takeWatchdogMarker(marker));
  TEST_ASSERT_EQUAL_UINT32(0, marker.value);
  TEST_ASSERT_EQUAL_UINT32(0, marker.check);
  TEST_ASSERT_FALSE(takeWatchdogMarker(marker));
}

void test_recovered_stall_clears_the_marker() {
  ResetMarker marker = {0, 0};
  armWatchdogMarker(marker);
  clearResetMarker(marker);
  TEST_ASSERT_FALSE(takeWatchdogMarker(marker));
}

void test_power_on_ram_patterns_are_not_a_watchdog_marker() {
  const ResetMarker patterns[] = {
      {0, 0},
      {0xFFFFFFFFUL, 0xFFFFFFFFUL},
      {kWatchdogMarker, kWatchdogMarker},
      {kWatchdogMarker, 0},
      {kWatchdogMarker, 0xFFFFFFFFUL},
      {static_cast<uint32_t>(~kWatchdogMarker), kWatchdogMarker},
      {kWatchdogMarker ^ 1UL, static_cast<uint32_t>(~kWatchdogMarker)},
  };
  for (size_t i = 0; i < sizeof(patterns) / sizeof(patterns[0]); ++i) {
    ResetMarker marker = patterns[i];
    TEST_ASSERT_FALSE(takeWatchdogMarker(marker));
    TEST_ASSERT_EQUAL_UINT32(0, marker.value);
  }
}

void test_controller_replies_to_new_state_frames_only() {
  Controller controller;
  controller.begin(0x00A1, 'E', 0);
  char reply[kMaxFrameBytes];
  uint8_t length = feed(controller, kStateVector, 100, reply);
  expectReport(reply, length, "GG1 R 00A1 0001 7F3A 0001 E 00 00 00");
  Demand demand = controller.demand(100);
  TEST_ASSERT_EQUAL_CHAR('U', demand.glucose);
  TEST_ASSERT_TRUE(demand.fault);
  TEST_ASSERT_FALSE(demand.linkLost);
  TEST_ASSERT_EQUAL_UINT8(0, feed(controller, kStateVector, 200, reply));
  TEST_ASSERT_EQUAL_UINT8(0, feed(controller, "GG1 S 7F3A 0002 U 1*03EE", 300, reply));
  TEST_ASSERT_EQUAL_UINT8(0, feed(controller, kQuietVector, 400, reply));
  TEST_ASSERT_EQUAL_UINT16(3, controller.rejectedFrames());

  Controller fresh;
  fresh.begin(0x00A1, 'E', 0);
  length = feed(fresh, kQuietVector, 400, reply);
  expectReport(reply, length, "GG1 R 00A1 0001 7F3A FFFF E 00 00 00");
  demand = fresh.demand(400);
  TEST_ASSERT_EQUAL_CHAR('-', demand.glucose);
  TEST_ASSERT_FALSE(demand.fault);
}

void test_controller_rejects_truncated_frame_joined_with_next() {
  const uint8_t cuts[] = {sizeof(kStateVector) - 1, 10};
  for (uint8_t cut : cuts) {
    Controller controller;
    controller.begin(0x00A1, 'E', 0);
    char reply[kMaxFrameBytes];
    for (uint8_t i = 0; i < cut; ++i) {
      TEST_ASSERT_EQUAL_UINT8(0, controller.receive(static_cast<uint8_t>(kStateVector[i]), 100,
                                                    reply));
    }
    TEST_ASSERT_EQUAL_UINT8(0, feedState(controller, 2, 200, reply));
    TEST_ASSERT_EQUAL_UINT16(1, controller.rejectedFrames());
    TEST_ASSERT_TRUE(controller.demand(kStartupGraceMs).linkLost);
    TEST_ASSERT_TRUE(feedState(controller, 3, kStartupGraceMs + 100, reply) > 0);
    TEST_ASSERT_FALSE(controller.demand(kStartupGraceMs + 100).linkLost);
  }
}

void test_controller_link_loss_is_audible_without_the_pi() {
  Controller controller;
  controller.begin(1, 'P', 0);
  TEST_ASSERT_FALSE(controller.demand(kStartupGraceMs - 1).fault);
  const Demand demand = controller.demand(kStartupGraceMs);
  TEST_ASSERT_TRUE(demand.fault);
  TEST_ASSERT_TRUE(demand.linkLost);
  TEST_ASSERT_EQUAL_CHAR('-', demand.glucose);
}

void test_controller_corrupted_frames_do_not_keep_link_alive() {
  Controller controller;
  controller.begin(1, 'E', 0);
  char reply[kMaxFrameBytes];
  TEST_ASSERT_TRUE(feedState(controller, 1, 1000, reply) > 0);
  for (uint32_t t = 2000; t <= 7000; t += 1000) {
    TEST_ASSERT_EQUAL_UINT8(0, feed(controller, "GG1 S 7F3A 0009 U 1*0000", t, reply));
    TEST_ASSERT_EQUAL_UINT8(0, feed(controller, "GG2 S 7F3A 0001 U 1*32C8", t, reply));
  }
  TEST_ASSERT_TRUE(controller.demand(6001).linkLost);
}

void test_controller_local_long_press_silences_for_sixty_seconds() {
  Controller controller;
  controller.begin(1, 'E', 0);
  TEST_ASSERT_TRUE(controller.demand(20000).fault);
  press(controller, 20000, 2500);
  const uint32_t silencedAt = 20000 + 2500 + kDebounceMs;
  TEST_ASSERT_FALSE(controller.demand(silencedAt).fault);
  press(controller, 40000, 2500);
  TEST_ASSERT_FALSE(controller.demand(silencedAt + kLocalSilenceMs - 1).fault);
  TEST_ASSERT_TRUE(controller.demand(silencedAt + kLocalSilenceMs).fault);
  TEST_ASSERT_TRUE(controller.demand(silencedAt + kLocalSilenceMs).linkLost);
}

void test_controller_forwards_presses_only_while_linked() {
  Controller controller;
  controller.begin(0x00A1, 'E', 0);
  char reply[kMaxFrameBytes];
  press(controller, 20000, 200);
  TEST_ASSERT_TRUE(feedState(controller, 1, 30000, reply) > 0);
  press(controller, 30100, 200);
  press(controller, 31000, 2500);
  uint8_t length = feedState(controller, 2, 34000, reply);
  expectReport(reply, length, "GG1 R 00A1 0002 7F3A 0002 E 00 01 01");
}

void test_controller_reports_stuck_button() {
  Controller controller;
  controller.begin(0x00A1, 'E', 0);
  char reply[kMaxFrameBytes];
  feedState(controller, 1, 100, reply);
  controller.button(true, 200);
  controller.button(true, 200 + kStuckMs);
  uint8_t length = feedState(controller, 2, 200 + kStuckMs, reply);
  expectReport(reply, length, "GG1 R 00A1 0002 7F3A 0002 E 01 00 00");
}

int main() {
  UNITY_BEGIN();
  RUN_TEST(test_crc_check_value);
  RUN_TEST(test_parse_shared_state_vectors);
  RUN_TEST(test_format_shared_report_vectors);
  RUN_TEST(test_parse_rejections);
  RUN_TEST(test_seq_newer_wraps);
  RUN_TEST(test_line_reader_bounds_and_recovers);
  RUN_TEST(test_startup_grace);
  RUN_TEST(test_heartbeat_timeout_boundary);
  RUN_TEST(test_timeout_latches_across_millis_wrap);
  RUN_TEST(test_duplicates_and_replays_never_refresh_liveness);
  RUN_TEST(test_sequence_wrap_and_pi_restart_resync);
  RUN_TEST(test_local_silence_is_bounded_not_extendable_and_rearms);
  RUN_TEST(test_button_debounce_ignores_glitches);
  RUN_TEST(test_button_short_and_long_boundaries);
  RUN_TEST(test_stuck_button_reports_and_never_acknowledges);
  RUN_TEST(test_reset_cause_priority);
  RUN_TEST(test_cleared_flags_report_watchdog_only_from_the_marker);
  RUN_TEST(test_watchdog_marker_is_reported_once);
  RUN_TEST(test_recovered_stall_clears_the_marker);
  RUN_TEST(test_power_on_ram_patterns_are_not_a_watchdog_marker);
  RUN_TEST(test_controller_replies_to_new_state_frames_only);
  RUN_TEST(test_controller_rejects_truncated_frame_joined_with_next);
  RUN_TEST(test_controller_link_loss_is_audible_without_the_pi);
  RUN_TEST(test_controller_corrupted_frames_do_not_keep_link_alive);
  RUN_TEST(test_controller_local_long_press_silences_for_sixty_seconds);
  RUN_TEST(test_controller_forwards_presses_only_while_linked);
  RUN_TEST(test_controller_reports_stuck_button);
  return UNITY_END();
}
