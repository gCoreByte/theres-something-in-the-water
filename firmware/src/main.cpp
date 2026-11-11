// Firmware for the SparkFun ESP32 Thing Plus C with the AS7265X spectral triad.
// Based on plasticdebris/triad_spectrometer and SWAN/arduino/triad_spectroscope.
//
// Each frame is five back-to-back readings: LEDs off, then all LEDs, UV, IR and white.
// The LEDs-off reading lets the host subtract room light and sensor offset from every lit reading.
// Every reading goes to serial as one line and, when WiFi is up, to the upload server.
// The server is found with mDNS: ./server on the laptop advertises _tsitw-upload._tcp.

#include <Arduino.h>
#include <ESPmDNS.h>
#include <FastLED.h>
#include <HTTPClient.h>
#include <WiFi.h>
#include <Wire.h>

#include "SparkFun_AS7265X.h"

const char* WIFI_SSID = "the-water";
const char* WIFI_PASSWORD = "something-in-the-water";
const char* MDNS_HOSTNAME = "tsitw-sensor";
const char* UPLOAD_SERVICE = "tsitw-upload";
const char* UPLOAD_PATH = "/upload";
const unsigned long WIFI_TIMEOUT_MS = 10000;
const unsigned long UPLOAD_TIMEOUT_MS = 1000;
const unsigned long DISCOVERY_RETRY_MS = 10000;
const int FAILURES_BEFORE_REDISCOVERY = 5;

// 58 cycles x 2.78 ms = 161 ms integration; all 18 channels take two integrations per reading.
const uint8_t INTEGRATION_CYCLES = 58;
const int CHANNEL_COUNT = 18;
const float SATURATED = -1;

const int STATUS_LED_PIN = 2;
const int BLUE_LED_PIN = 13;

struct Mode {
  const char* name;
  bool white;
  bool ir;
  bool uv;
};

const Mode MODES[] = {
  {"OFF", false, false, false},
  {"WHITE_IR_UV", true, true, true},
  {"UV", false, false, true},
  {"IR", false, true, false},
  {"WHITE", true, false, false},
};
const int MODE_COUNT = sizeof(MODES) / sizeof(MODES[0]);

struct Reading {
  unsigned long frame;
  unsigned long timestamp;
  char mode[16];
  float temperatures[3];
  float channels[CHANNEL_COUNT];
};

AS7265X sensor;
CRGB statusLed[1];
QueueHandle_t uploadQueue;
unsigned long frameNumber = 0;
bool wifiConnected = false;
bool uploadQueueFull = false;
volatile bool serverFound = false;
char uploadUrl[64] = "";

void connectWifi();
void reportWifiChanges();
void setBulbs(const Mode& mode);
void takeReading(const Mode& mode);
bool readChannels(float* channels);
void printReading(const Reading& reading);
void queueUpload(const Reading& reading);
void uploadTask(void* parameter);
bool discoverServer();
bool upload(const Reading& reading);

void setup() {
  FastLED.addLeds<WS2812, STATUS_LED_PIN, GRB>(statusLed, 1);
  statusLed[0] = CRGB::Black;
  FastLED.show();
  pinMode(BLUE_LED_PIN, OUTPUT);
  digitalWrite(BLUE_LED_PIN, LOW);

  Serial.begin(115200);
  delay(1000);
  Serial.println("\nStarting setup...");

  connectWifi();

  Wire.begin();
  while (!sensor.begin()) {
    Serial.println("Sensor not connected.");
    delay(500);
  }
  Wire.setClock(400000);
  sensor.disableIndicator();
  sensor.setIntegrationCycles(INTEGRATION_CYCLES);
  sensor.setBulbCurrent(AS7265X_LED_CURRENT_LIMIT_12_5MA, AS7265x_LED_WHITE);
  sensor.setBulbCurrent(AS7265X_LED_CURRENT_LIMIT_12_5MA, AS7265x_LED_IR);
  sensor.setBulbCurrent(AS7265X_LED_CURRENT_LIMIT_12_5MA, AS7265x_LED_UV);
  setBulbs(MODES[0]);
  Serial.printf("Integration cycles set to: %d\n", INTEGRATION_CYCLES);

  uploadQueue = xQueueCreate(20, sizeof(Reading));
  xTaskCreatePinnedToCore(uploadTask, "upload", 8192, NULL, 1, NULL, 0);

  Serial.println("Starting data collection...");
}

void loop() {
  frameNumber++;
  for (int i = 0; i < MODE_COUNT; i++) {
    takeReading(MODES[i]);
  }
  reportWifiChanges();
}

void connectWifi() {
  Serial.printf("Connecting to WiFi '%s'", WIFI_SSID);
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  unsigned long started = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - started < WIFI_TIMEOUT_MS) {
    delay(500);
    Serial.print(".");
  }
  Serial.println();
  reportWifiChanges();
  if (wifiConnected) {
    return;
  }
  if (WiFi.status() == WL_NO_SSID_AVAIL) {
    Serial.printf("[Warning] WiFi '%s' not found. Measuring without upload.\n", WIFI_SSID);
  } else {
    Serial.printf("[Warning] Could not join WiFi '%s'. Measuring without upload.\n", WIFI_SSID);
  }
}

void reportWifiChanges() {
  bool connected = WiFi.status() == WL_CONNECTED;
  if (connected == wifiConnected) {
    return;
  }
  wifiConnected = connected;
  if (connected) {
    Serial.printf("Connected to WiFi with IP Address: %s\n", WiFi.localIP().toString().c_str());
  } else {
    Serial.println("[Warning] WiFi connection lost. Measuring without upload.");
  }
}

void setBulbs(const Mode& mode) {
  mode.white ? sensor.enableBulb(AS7265x_LED_WHITE) : sensor.disableBulb(AS7265x_LED_WHITE);
  mode.ir ? sensor.enableBulb(AS7265x_LED_IR) : sensor.disableBulb(AS7265x_LED_IR);
  mode.uv ? sensor.enableBulb(AS7265x_LED_UV) : sensor.disableBulb(AS7265x_LED_UV);
}

void takeReading(const Mode& mode) {
  Reading reading;
  reading.frame = frameNumber;
  reading.timestamp = millis();
  strncpy(reading.mode, mode.name, sizeof(reading.mode) - 1);
  reading.mode[sizeof(reading.mode) - 1] = '\0';

  setBulbs(mode);
  sensor.takeMeasurements();
  reading.temperatures[0] = sensor.getTemperature(0);
  reading.temperatures[1] = sensor.getTemperature(1);
  reading.temperatures[2] = sensor.getTemperature(2);
  setBulbs(MODES[0]);

  if (readChannels(reading.channels)) {
    Serial.printf("[Warning] Saturated channels in the %s reading; values set to -1.\n", mode.name);
  }
  printReading(reading);
  queueUpload(reading);
}

bool readChannels(float* channels) {
  float values[CHANNEL_COUNT] = {
    sensor.getCalibratedA(), sensor.getCalibratedB(), sensor.getCalibratedC(),
    sensor.getCalibratedD(), sensor.getCalibratedE(), sensor.getCalibratedF(),
    sensor.getCalibratedG(), sensor.getCalibratedH(), sensor.getCalibratedR(),
    sensor.getCalibratedI(), sensor.getCalibratedS(), sensor.getCalibratedJ(),
    sensor.getCalibratedT(), sensor.getCalibratedU(), sensor.getCalibratedV(),
    sensor.getCalibratedW(), sensor.getCalibratedK(), sensor.getCalibratedL(),
  };
  bool saturated = false;
  for (int i = 0; i < CHANNEL_COUNT; i++) {
    if (values[i] > 65535 || values[i] < 0) {
      values[i] = SATURATED;
      saturated = true;
    }
    channels[i] = values[i];
  }
  return saturated;
}

void printReading(const Reading& reading) {
  char line[512];
  int length = snprintf(
    line, sizeof(line), "Frame %lu Time %lu [%s] Temps (NIR:%.1f, VIS:%.1f, UV:%.1f)°C: ",
    reading.frame, reading.timestamp, reading.mode,
    reading.temperatures[0], reading.temperatures[1], reading.temperatures[2]
  );
  for (int i = 0; i < CHANNEL_COUNT && length < (int)sizeof(line); i++) {
    length += snprintf(line + length, sizeof(line) - length, i ? ",%.2f" : "%.2f", reading.channels[i]);
  }
  Serial.println(line);
}

void queueUpload(const Reading& reading) {
  if (!wifiConnected || !serverFound) {
    return;
  }
  bool queued = xQueueSend(uploadQueue, &reading, 0) == pdTRUE;
  if (!queued && !uploadQueueFull) {
    Serial.println("[Warning] Upload queue full; readings are still printed but not uploaded.");
  }
  uploadQueueFull = !queued;
}

void uploadTask(void* parameter) {
  Reading reading;
  unsigned long lastDiscovery = 0;
  bool discoveredOnce = false;
  bool warnedMissing = false;
  int failures = 0;
  while (true) {
    bool received = xQueueReceive(uploadQueue, &reading, pdMS_TO_TICKS(1000)) == pdTRUE;
    if (WiFi.status() != WL_CONNECTED) {
      continue;
    }
    if (!serverFound) {
      if (discoveredOnce && millis() - lastDiscovery < DISCOVERY_RETRY_MS) {
        continue;
      }
      discoveredOnce = true;
      lastDiscovery = millis();
      if (discoverServer()) {
        warnedMissing = false;
        failures = 0;
      } else if (!warnedMissing) {
        Serial.printf(
          "[Warning] No upload server found (_%s._tcp). Run ./server on the laptop; retrying every %lu s.\n",
          UPLOAD_SERVICE, DISCOVERY_RETRY_MS / 1000
        );
        warnedMissing = true;
      }
      continue;
    }
    if (!received) {
      continue;
    }
    failures = upload(reading) ? 0 : failures + 1;
    if (failures == 1) {
      Serial.printf("[Warning] Upload to %s failed; readings are still printed.\n", uploadUrl);
    }
    if (failures >= FAILURES_BEFORE_REDISCOVERY) {
      Serial.println("[Warning] Upload server not answering; looking for it again.");
      serverFound = false;
      failures = 0;
      xQueueReset(uploadQueue);
    }
  }
}

bool discoverServer() {
  static bool mdnsStarted = false;
  if (!mdnsStarted) {
    mdnsStarted = MDNS.begin(MDNS_HOSTNAME);
    if (!mdnsStarted) {
      return false;
    }
  }
  int count = MDNS.queryService(UPLOAD_SERVICE, "tcp");
  if (count <= 0) {
    return false;
  }
  IPAddress local = WiFi.localIP();
  IPAddress mask = WiFi.subnetMask();
  int chosen = 0;
  for (int i = 0; i < count; i++) {
    IPAddress candidate = MDNS.IP(i);
    if ((uint32_t(candidate) & uint32_t(mask)) == (uint32_t(local) & uint32_t(mask))) {
      chosen = i;
      break;
    }
  }
  snprintf(
    uploadUrl, sizeof(uploadUrl), "http://%s:%u%s",
    MDNS.IP(chosen).toString().c_str(), MDNS.port(chosen), UPLOAD_PATH
  );
  Serial.printf("Upload server found: %s\n", uploadUrl);
  serverFound = true;
  return true;
}

bool upload(const Reading& reading) {
  char url[768];
  int length = snprintf(
    url, sizeof(url), "%s?frame=%lu&timestamp=%lu&combination=%s&t_nir=%.2f&t_vis=%.2f&t_uv=%.2f",
    uploadUrl, reading.frame, reading.timestamp, reading.mode,
    reading.temperatures[0], reading.temperatures[1], reading.temperatures[2]
  );
  const char* names = "ABCDEFGHRISJTUVWKL";
  for (int i = 0; i < CHANNEL_COUNT && length < (int)sizeof(url); i++) {
    length += snprintf(url + length, sizeof(url) - length, "&%cval=%.2f", names[i], reading.channels[i]);
  }

  HTTPClient http;
  http.setConnectTimeout(UPLOAD_TIMEOUT_MS);
  http.setTimeout(UPLOAD_TIMEOUT_MS);
  http.begin(url);
  int status = http.GET();
  http.end();
  return status > 0;
}
