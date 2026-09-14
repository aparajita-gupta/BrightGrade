/*
  esp32_display.ino

  Ambient deadline display for a MELIFE ESP32-S dev board + 0.96"
  SSD1306 OLED (I2C, 128x64). Polls Todoist's own REST API directly
  over WiFi (not this project's Mac/Python backend) and shows the
  single next-due task with a live countdown.

  Setup:
    1. Arduino IDE -> Boards Manager -> install "esp32" (Espressif),
       select board "ESP32 Dev Module".
    2. Library Manager -> install "Adafruit SSD1306", "Adafruit GFX
       Library", "ArduinoJson" (v7+).
    3. Copy secrets.h.example -> secrets.h in this same folder and
       fill in your real WiFi + Todoist API token.
    4. Wiring: OLED SDA -> ESP32 GPIO21, SCL -> GPIO22, VCC -> 3V3,
       GND -> GND (default ESP32 I2C pins -- no extra config needed).
    5. Flash, then open Serial Monitor at 115200 baud to watch WiFi
       connect + fetch/parse logs while the display comes up.

  "Next due" is computed across ALL active Todoist tasks with a due
  date, not filtered to course-labeled ones -- see prompt.md's item #7
  notes if you want to add a label filter later.
*/

#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <Wire.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>

#include "secrets.h"

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------

#define SCREEN_WIDTH 128
#define SCREEN_HEIGHT 64
#define OLED_RESET -1
#define SCREEN_ADDRESS 0x3C

const unsigned long FETCH_INTERVAL_MS = 10UL * 60UL * 1000UL;  // 10 min
const unsigned long RENDER_INTERVAL_MS = 30UL * 1000UL;        // 30 s

Adafruit_SSD1306 display(SCREEN_WIDTH, SCREEN_HEIGHT, &Wire, OLED_RESET);

// Cached "next due" state, refreshed every FETCH_INTERVAL_MS.
bool hasTask = false;
String cachedTitle = "";
time_t cachedDueEpoch = 0;

unsigned long lastFetchAt = 0;
unsigned long lastRenderAt = 0;

// ---------------------------------------------------------------------------
// Date parsing -- no libc timegm() dependency, since ESP32 core libc
// timezone/DST handling varies by version and I can't test-compile this
// against your exact board package. Howard Hinnant's days-from-civil
// algorithm is self-contained and portable.
// ---------------------------------------------------------------------------

long daysFromCivil(int y, int m, int d) {
  y -= m <= 2;
  long era = (y >= 0 ? y : y - 399) / 400;
  unsigned yoe = (unsigned)(y - era * 400);
  unsigned doy = (153 * (m + (m > 2 ? -3 : 9)) + 2) / 5 + d - 1;
  unsigned doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
  return era * 146097 + (long)doe - 719468;  // days since 1970-01-01
}

// Parses "YYYY-MM-DD" or "YYYY-MM-DDTHH:MM:SS[.sss][Z|+HH:MM|-HH:MM]".
// Date-only strings are treated as 00:00:00 UTC that day.
bool parseIso8601(const char *s, time_t *outEpoch) {
  int y, mo, d, h = 0, mi = 0, se = 0;
  int matched = sscanf(s, "%d-%d-%dT%d:%d:%d", &y, &mo, &d, &h, &mi, &se);
  if (matched < 3) return false;

  long days = daysFromCivil(y, mo, d);
  long localSecs = days * 86400L + h * 3600L + mi * 60L + se;

  // Adjust for a numeric UTC offset if present (+HH:MM / -HH:MM). A
  // trailing "Z" needs no adjustment. Todoist's own account-timezone
  // datetimes normally come back as UTC ("Z") already.
  const char *tzPos = strchr(s, 'T');
  if (tzPos) {
    const char *plus = strchr(tzPos, '+');
    const char *minus = strrchr(tzPos, '-');
    const char *sign = plus ? plus : minus;
    if (sign) {
      int offH = 0, offM = 0;
      sscanf(sign + 1, "%d:%d", &offH, &offM);
      long offSecs = offH * 3600L + offM * 60L;
      localSecs += (sign == minus) ? offSecs : -offSecs;
    }
  }

  *outEpoch = (time_t)localSecs;
  return true;
}

String formatCountdown(long secondsRemaining) {
  if (secondsRemaining < 0) return "OVERDUE";
  long days = secondsRemaining / 86400L;
  long hours = (secondsRemaining % 86400L) / 3600L;
  long minutes = (secondsRemaining % 3600L) / 60L;

  char buf[24];
  if (days > 0) {
    snprintf(buf, sizeof(buf), "%ldd %ldh", days, hours);
  } else if (hours > 0) {
    snprintf(buf, sizeof(buf), "%ldh %ldm", hours, minutes);
  } else {
    snprintf(buf, sizeof(buf), "%ldm", minutes);
  }
  return String(buf);
}

// ---------------------------------------------------------------------------
// WiFi / display setup
// ---------------------------------------------------------------------------

void connectWiFi() {
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

  display.clearDisplay();
  display.setCursor(0, 0);
  display.println("Connecting to WiFi...");
  display.display();

  Serial.print("Connecting to WiFi");
  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
  }
  Serial.println();
  Serial.print("Connected, IP: ");
  Serial.println(WiFi.localIP());

  // Sync system clock over NTP (UTC) so we have a real "now" to diff
  // against Todoist's due timestamps.
  configTime(0, 0, "pool.ntp.org", "time.nist.gov");
  Serial.println("Waiting for NTP time sync...");
  time_t now = time(nullptr);
  while (now < 100000) {  // still near epoch = not synced yet
    delay(500);
    now = time(nullptr);
  }
  Serial.print("Time synced: ");
  Serial.println((long)now);
}

// ---------------------------------------------------------------------------
// Fetch + parse the earliest-due active Todoist task
// ---------------------------------------------------------------------------

void fetchNextDueTask() {
  WiFiClientSecure client;
  client.setInsecure();  // no cert pinning -- personal hobby device, see prompt.md item #7 notes

  HTTPClient http;
  http.begin(client, "https://api.todoist.com/api/v1/tasks?limit=200");
  http.addHeader("Authorization", "Bearer " TODOIST_API_TOKEN);

  int code = http.GET();
  if (code != 200) {
    Serial.print("Todoist fetch failed, HTTP ");
    Serial.println(code);
    http.end();
    return;
  }

  // Filter document: only keep the fields we actually need, so the
  // parsed JSON doesn't have to hold every task field in RAM. Todoist's
  // API has no separate "datetime" field -- due.date alone holds either
  // a bare "YYYY-MM-DD" (all-day task) or a full ISO datetime with a
  // "Z" suffix (timed task), confirmed against the real live API.
  JsonDocument filter;
  JsonArray filterResults = filter["results"].to<JsonArray>();
  JsonObject filterItem = filterResults.add<JsonObject>();
  filterItem["content"] = true;
  filterItem["due"]["date"] = true;

  JsonDocument doc;
  DeserializationError err = deserializeJson(doc, http.getStream(), DeserializationOption::Filter(filter));
  http.end();

  if (err) {
    Serial.print("JSON parse failed: ");
    Serial.println(err.c_str());
    return;
  }

  bool found = false;
  time_t bestEpoch = 0;
  String bestTitle;

  for (JsonObject task : doc["results"].as<JsonArray>()) {
    JsonObject due = task["due"];
    if (due.isNull()) continue;

    const char *raw = due["date"].as<const char *>();
    if (!raw) continue;

    time_t epoch;
    if (!parseIso8601(raw, &epoch)) continue;

    if (!found || epoch < bestEpoch) {
      found = true;
      bestEpoch = epoch;
      bestTitle = task["content"].as<String>();
    }
  }

  if (found) {
    hasTask = true;
    cachedDueEpoch = bestEpoch;
    cachedTitle = bestTitle;
    Serial.print("Next due: ");
    Serial.print(cachedTitle);
    Serial.print(" @ epoch ");
    Serial.println((long)cachedDueEpoch);
  } else {
    hasTask = false;
    Serial.println("No dated active tasks found.");
  }
}

// ---------------------------------------------------------------------------
// Render
// ---------------------------------------------------------------------------

void renderDisplay() {
  display.clearDisplay();
  display.setTextSize(1);
  display.setTextColor(SSD1306_WHITE);

  display.setCursor(0, 0);
  display.println("NEXT DUE");
  display.drawLine(0, 10, SCREEN_WIDTH, 10, SSD1306_WHITE);

  if (!hasTask) {
    display.setCursor(0, 20);
    display.println("Nothing dated");
    display.setCursor(0, 32);
    display.println("upcoming.");
  } else {
    display.setCursor(0, 20);
    display.println(cachedTitle);

    long remaining = (long)(cachedDueEpoch - time(nullptr));
    display.setCursor(0, 40);
    display.setTextSize(2);
    display.println(formatCountdown(remaining));
  }

  display.display();
}

// ---------------------------------------------------------------------------
// Arduino entry points
// ---------------------------------------------------------------------------

void setup() {
  Serial.begin(115200);

  Wire.begin();
  if (!display.begin(SSD1306_SWITCHCAPVCC, SCREEN_ADDRESS)) {
    Serial.println("SSD1306 init failed -- check wiring/I2C address");
    for (;;) delay(1000);
  }
  display.clearDisplay();
  display.display();

  connectWiFi();

  fetchNextDueTask();
  lastFetchAt = millis();
  renderDisplay();
  lastRenderAt = millis();
}

void loop() {
  unsigned long now = millis();

  if (now - lastFetchAt >= FETCH_INTERVAL_MS) {
    fetchNextDueTask();
    lastFetchAt = now;
  }

  if (now - lastRenderAt >= RENDER_INTERVAL_MS) {
    renderDisplay();
    lastRenderAt = now;
  }

  delay(1000);
}
