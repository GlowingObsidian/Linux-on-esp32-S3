/* Read-only Nano ESP32 SPI SD connection test. No SD/FAT library, no writes.
 * Tests the existing Linux ext2 card. This tests wiring/data integrity, not DMA.
 */
#include <Arduino.h>
#include <SPI.h>

constexpr uint8_t CS_PIN = D10; // board alias: correct in either pin-numbering mode
static uint32_t busHz = 400000;
static bool blockAddressed;
static uint8_t referenceMBR[512], referenceSuper[512], dataBlock[512];

uint8_t commandCRC(const uint8_t *p, size_t n) {
  uint8_t crc = 0;
  while (n--) {
    crc ^= *p++;
    for (unsigned b = 0; b < 8; ++b)
      crc = (crc & 0x80) ? uint8_t((crc << 1) ^ 0x12) : uint8_t(crc << 1);
  }
  return crc | 1;
}
uint16_t dataCRC(const uint8_t *p, size_t n) {
  uint16_t crc = 0;
  while (n--) {
    crc ^= uint16_t(*p++) << 8;
    for (unsigned b = 0; b < 8; ++b)
      crc = (crc & 0x8000) ? uint16_t((crc << 1) ^ 0x1021) : uint16_t(crc << 1);
  }
  return crc;
}
uint32_t crc32(const uint8_t *p, size_t n) {
  uint32_t crc = ~0U;
  while (n--) {
    crc ^= *p++;
    for (unsigned b = 0; b < 8; ++b)
      crc = (crc >> 1) ^ ((crc & 1) ? 0xedb88320U : 0);
  }
  return ~crc;
}
uint32_t little32(const uint8_t *p) {
  return uint32_t(p[0]) | uint32_t(p[1]) << 8 |
         uint32_t(p[2]) << 16 | uint32_t(p[3]) << 24;
}
void deselectCard() {
  digitalWrite(CS_PIN, HIGH);
  SPI.transfer(0xff);
  SPI.endTransaction();
}
bool waitReady(uint32_t timeoutMs) {
  uint32_t start = millis();
  do {
    if (SPI.transfer(0xff) == 0xff) return true;
    yield();
  } while (millis() - start < timeoutMs);
  return false;
}
// Leaves CS selected, so the caller may read data before deselectCard().
uint8_t command(uint8_t cmd, uint32_t arg) {
  SPI.beginTransaction(SPISettings(busHz, MSBFIRST, SPI_MODE0));
  digitalWrite(CS_PIN, LOW);
  SPI.transfer(0xff);
  if (cmd != 0 && !waitReady(500)) return 0xff;
  uint8_t frame[6] = {uint8_t(0x40 | cmd), uint8_t(arg >> 24),
    uint8_t(arg >> 16), uint8_t(arg >> 8), uint8_t(arg), 0};
  frame[5] = commandCRC(frame, 5);
  for (uint8_t byte : frame) SPI.transfer(byte);
  for (unsigned i = 0; i < 16; ++i) {
    uint8_t r = SPI.transfer(0xff);
    if (!(r & 0x80)) return r;
  }
  return 0xff;
}
bool initializeCard() {
  busHz = 400000;
  SPI.beginTransaction(SPISettings(busHz, MSBFIRST, SPI_MODE0));
  digitalWrite(CS_PIN, HIGH);
  for (unsigned i = 0; i < 20; ++i) SPI.transfer(0xff);
  SPI.endTransaction();
  uint8_t r = 0xff;
  for (unsigned i = 0; i < 10; ++i) {
    r = command(0, 0); deselectCard();
    if (r == 1) break;
    delay(20);
  }
  Serial.printf("CMD0: R1=%02x (expected 01)\n", r);
  if (r != 1) return false;
  r = command(8, 0x1aa);
  uint32_t r7 = 0;
  if (r == 1) for (unsigned i = 0; i < 4; ++i) r7 = (r7 << 8) | SPI.transfer(0xff);
  deselectCard();
  Serial.printf("CMD8: R1=%02x R7=%08lx\n", r, (unsigned long)r7);
  bool v2 = r == 1;
  if (v2 ? (r7 != 0x1aa) : (r != 5)) return false;
  uint32_t start = millis();
  do {
    r = command(55, 0); deselectCard();
    if (r > 1) return false;
    r = command(41, v2 ? 0x40000000 : 0); deselectCard();
    if (r == 0) break;
    if (r != 1) return false;
    delay(10);
  } while (millis() - start < 4000);
  Serial.printf("ACMD41: R1=%02x (expected 00)\n", r);
  if (r != 0) return false;
  r = command(58, 0);
  uint32_t ocr = 0;
  for (unsigned i = 0; i < 4; ++i) ocr = (ocr << 8) | SPI.transfer(0xff);
  deselectCard();
  Serial.printf("CMD58: R1=%02x OCR=%08lx\n", r, (unsigned long)ocr);
  if (r || !(ocr & 0x80000000U) || !(ocr & 0x00780000U)) return false;
  blockAddressed = (ocr & 0x40000000U) != 0;
  Serial.println(blockAddressed ? "Addressing: SDHC/SDXC blocks" : "Addressing: SDSC bytes");
  if (!blockAddressed) {
    r = command(16, 512); deselectCard();
    if (r) return false;
  }
  return true;
}
bool readSector(uint32_t lba, uint8_t *dst) {
  if (!blockAddressed && lba > 0x7fffffU) return false;
  uint8_t r = command(17, blockAddressed ? lba : lba * 512);
  if (r) {
    deselectCard();
    Serial.printf("FAIL CMD17: LBA=%lu R1=%02x\n", (unsigned long)lba, r);
    return false;
  }
  uint32_t start = millis();
  uint8_t token;
  do { token = SPI.transfer(0xff); } while (token == 0xff && millis() - start < 1000);
  if (token != 0xfe) {
    deselectCard(); Serial.printf("FAIL data token: %02x\n", token); return false;
  }
  for (unsigned i = 0; i < 512; ++i) dst[i] = SPI.transfer(0xff);
  uint16_t wireCRC = uint16_t(SPI.transfer(0xff)) << 8;
  wireCRC |= SPI.transfer(0xff);
  deselectCard();
  uint16_t computed = dataCRC(dst, 512);
  if (wireCRC != computed) {
    Serial.printf("FAIL data CRC16: LBA=%lu got=%04x expected=%04x\n",
      (unsigned long)lba, wireCRC, computed);
    return false;
  }
  return true;
}
void setup() {
  Serial.begin(115200);
  uint32_t start = millis();
  while (!Serial && millis() - start < 8000) delay(10);
  delay(500);
  Serial.println("\nNano ESP32 RAW READ-ONLY SD test (Linux ext2 card)");
  Serial.println("Pins: D10 CS=GPIO21, D11 MOSI=GPIO38, D12 MISO=GPIO47, D13 SCK=GPIO48");
  pinMode(CS_PIN, OUTPUT); digitalWrite(CS_PIN, HIGH);
  pinMode(MISO, INPUT_PULLUP);
  SPI.begin();
  delay(100);
  if (!initializeCard()) { Serial.println("FAIL: raw SD initialization"); return; }
  if (!readSector(0, referenceMBR)) return;
  Serial.printf("MBR: signature=%02x%02x CRC32=%08lx\n", referenceMBR[511],
    referenceMBR[510], (unsigned long)crc32(referenceMBR, 512));
  if (referenceMBR[510] != 0x55 || referenceMBR[511] != 0xaa) {
    Serial.println("FAIL: missing MBR signature"); return;
  }
  uint32_t partition = little32(referenceMBR + 454);
  Serial.printf("Partition 1: type=%02x start LBA=%lu\n", referenceMBR[450], (unsigned long)partition);
  if (referenceMBR[450] != 0x83 || partition != 2048 || crc32(referenceMBR,512) != 0x6f625234U) {
    Serial.println("FAIL: card does not match our written Linux image"); return;
  }
  uint32_t superLBA = partition + 2;
  if (!readSector(superLBA, referenceSuper)) return;
  Serial.printf("ext2: magic=%02x%02x CRC32=%08lx\n", referenceSuper[57], referenceSuper[56],
    (unsigned long)crc32(referenceSuper,512));
  if (referenceSuper[56] != 0x53 || referenceSuper[57] != 0xef) {
    Serial.println("FAIL: missing ext2 superblock signature"); return;
  }
  const uint32_t speeds[] = {400000, 1000000, 4000000, 10000000, 20000000};
  for (uint32_t hz : speeds) {
    busHz = hz;
    for (unsigned repeat = 0; repeat < 32; ++repeat) {
      if (!readSector(0, dataBlock) || memcmp(dataBlock,referenceMBR,512)) {
        Serial.printf("FAIL: MBR repeat %u at %lu Hz\n",repeat,(unsigned long)hz); return;
      }
      if (!readSector(superLBA, dataBlock) || memcmp(dataBlock,referenceSuper,512)) {
        Serial.printf("FAIL: ext2 repeat %u at %lu Hz\n",repeat,(unsigned long)hz); return;
      }
      yield();
    }
    Serial.printf("PASS %lu Hz: 64 reads, CRC16 and exact data match\n", (unsigned long)hz);
  }
  Serial.println("PASS ALL: raw SPI initialization + Linux MBR/ext2 reads + speed sweep");
  Serial.println("No card sectors were written. This test does not prove Linux or DMA correctness.");
}
void loop() { delay(1000); }
