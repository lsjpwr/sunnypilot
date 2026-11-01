#include <cstring>
#include <deque>
#include <tuple>

#include "common/tests/native_test.h"
#include "selfdrive/pandad/panda.h"

// Substitute only the hardware transport; Panda's production wrappers are linked unchanged.
static std::deque<std::string> log_chunks;
static bool read_error = false;
static std::vector<std::tuple<uint8_t, uint16_t, uint16_t>> writes;

PandaSpiHandle::PandaSpiHandle(std::string serial) : hw_serial(serial) {}
PandaSpiHandle::~PandaSpiHandle() = default;
std::vector<std::string> PandaSpiHandle::list() { return {}; }
int PandaSpiHandle::bulk_read(unsigned char, unsigned char *, int, unsigned int) { return 0; }
int PandaSpiHandle::bulk_write(unsigned char, unsigned char *, int, unsigned int) { return 0; }

int PandaSpiHandle::control_write(uint8_t request, uint16_t param1, uint16_t param2, unsigned int) {
  writes.emplace_back(request, param1, param2);
  return 0;
}

int PandaSpiHandle::control_read(uint8_t request, uint16_t param1, uint16_t param2,
                               unsigned char *data, uint16_t length, unsigned int) {
  if (request == 0xc1) {
    data[0] = (uint8_t)cereal::PandaState::PandaType::RED_PANDA;
    return 1;
  }
  CHECK(request == 0xb6);
  CHECK(param1 == 0);
  CHECK(param2 == 0);
  CHECK(length == 64);
  if (log_chunks.empty()) return read_error ? -1 : 0;
  const auto chunk = log_chunks.front();
  log_chunks.pop_front();
  CHECK(chunk.size() <= length);
  std::memcpy(data, chunk.data(), chunk.size());
  return chunk.size();
}

void test_host_protocol() {
  Panda panda("test-transport");
  log_chunks = {"Register 0x1234", " diverged\n"};
  CHECK(panda.serial_read() == "Register 0x1234 diverged\n");
  CHECK(panda.serial_read().empty());
  read_error = true;
  log_chunks = {"partial log\n"};
  CHECK(panda.serial_read() == "partial log\n");
  CHECK(panda.serial_read().empty());

  writes.clear();
  panda.set_alternative_experience(0x1234, 0x8009);
  panda.set_safety_model(cereal::CarParams::SafetyModel::TESLA, 6);
  panda.send_heartbeat(false, true);
  panda.send_heartbeat(true, false);
  const std::vector<std::tuple<uint8_t, uint16_t, uint16_t>> expected = {
    {0xdf, 0x1234, 0x8009}, {0xdc, (uint16_t)cereal::CarParams::SafetyModel::TESLA, 6},
    {0xf3, 0, 1}, {0xf3, 1, 0},
  };
  CHECK(writes == expected);
}

int main() {
  return run_native_test(test_host_protocol);
}
