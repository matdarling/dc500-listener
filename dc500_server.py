import socketserver
import sqlite3
import struct
import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread

TCP_PORT = 10560
WEB_PORT = 8000
DB_PATH = "dc500_data.sqlite"


def init_db():
    with sqlite3.connect(DB_PATH) as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS readings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                received_at TEXT NOT NULL,
                remote_ip TEXT,
                raw_hex TEXT NOT NULL,
                report_type INTEGER,
                people_count INTEGER,
                people_alarm INTEGER,
                battery_alarm INTEGER,
                battery_mv INTEGER,
                battery_v REAL,
                rsrp REAL,
                device_id TEXT,
                device_timestamp TEXT,
                frame_count INTEGER
            )
        """)


def parse_dc500(data: bytes):
    """
    DC500 packet format:
    80 00 15 <report_type> <packet_size> <payload> 81
    For normal heartbeat/alarm reports, payload is:
    people_count: 2 bytes
    people_count_status: 1 byte
    battery_status: 1 byte
    battery_voltage: 2 bytes, value * 10 mV
    RSRP: 4 bytes, little-endian float based on Dingtek example
    device_id: 8 bytes
    timestamp: 4 bytes Unix time
    frame_count: 2 bytes
    """

    result = {
        "report_type": None,
        "people_count": None,
        "people_alarm": None,
        "battery_alarm": None,
        "battery_mv": None,
        "battery_v": None,
        "rsrp": None,
        "device_id": None,
        "device_timestamp": None,
        "frame_count": None,
    }

    if len(data) < 6:
        return result

    if data[0] != 0x80 or data[-1] != 0x81:
        return result

    device_type = data[2]
    report_type = data[3]
    packet_size = data[4]
    payload = data[5:-1]

    result["report_type"] = report_type

    # DC500 is device type 0x15
    if device_type != 0x15:
        return result

    # Type 0x01 = alarm/abnormal report
    # Type 0x02 = heartbeat/reset report
    if report_type in (0x01, 0x02) and len(payload) >= 20:
        people_count = int.from_bytes(payload[0:2], "big")
        people_alarm = payload[2]
        battery_alarm = payload[3]

        # Manual example: 0166 = 358 * 10 mV = 3.58 V
        battery_raw = int.from_bytes(payload[4:6], "big")
        battery_mv = battery_raw * 10

        # Dingtek example appears little-endian for float RSRP
        try:
            rsrp = struct.unpack("<f", payload[6:10])[0]
        except Exception:
            rsrp = None

        device_id = payload[10:18].hex().upper()

        ts_raw = int.from_bytes(payload[18:22], "big")
        try:
            device_timestamp = datetime.datetime.fromtimestamp(
                ts_raw, tz=datetime.timezone.utc
            ).isoformat()
        except Exception:
            device_timestamp = None

        frame_count = int.from_bytes(payload[22:24], "big") if len(payload) >= 24 else None

        result.update({
            "people_count": people_count,
            "people_alarm": people_alarm,
            "battery_alarm": battery_alarm,
            "battery_mv": battery_mv,
            "battery_v": battery_mv / 1000,
            "rsrp": rsrp,
            "device_id": device_id,
            "device_timestamp": device_timestamp,
            "frame_count": frame_count,
        })

    return result


class DC500TCPHandler(socketserver.BaseRequestHandler):
    def handle(self):
        data = self.request.recv(1024)
        raw_hex = data.hex().upper()
        remote_ip = self.client_address[0]
        received_at = datetime.datetime.now(datetime.timezone.utc).isoformat()

        parsed = parse_dc500(data)

        print(f"\nFrom {remote_ip} at {received_at}")
        print(raw_hex)
        print(parsed)

        with sqlite3.connect(DB_PATH) as con:
            con.execute("""
                INSERT INTO readings (
                    received_at, remote_ip, raw_hex, report_type,
                    people_count, people_alarm, battery_alarm,
                    battery_mv, battery_v, rsrp, device_id,
                    device_timestamp, frame_count
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                received_at,
                remote_ip,
                raw_hex,
                parsed["report_type"],
                parsed["people_count"],
                parsed["people_alarm"],
                parsed["battery_alarm"],
                parsed["battery_mv"],
                parsed["battery_v"],
                parsed["rsrp"],
                parsed["device_id"],
                parsed["device_timestamp"],
                parsed["frame_count"],
            ))


class WebHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        with sqlite3.connect(DB_PATH) as con:
            rows = con.execute("""
                SELECT received_at, device_timestamp, device_id, people_count,
                       battery_v, rsrp, frame_count, raw_hex
                FROM readings
                ORDER BY id DESC
                LIMIT 100
            """).fetchall()

        html = """
        <html>
        <head>
            <title>DC500 People Counter</title>
            <meta http-equiv="refresh" content="60">
            <style>
                body { font-family: Arial, sans-serif; margin: 30px; }
                table { border-collapse: collapse; width: 100%; font-size: 14px; }
                th, td { border: 1px solid #ccc; padding: 6px; text-align: left; }
                th { background: #eee; }
                code { font-size: 11px; }
            </style>
        </head>
        <body>
            <h1>DC500 People Counter Data</h1>
            <p>Auto-refreshes every 10 seconds.</p>
            <table>
                <tr>
                    <th>Received UTC</th>
                    <th>Device Time UTC</th>
                    <th>Device ID</th>
                    <th>People Count</th>
                    <th>Battery V</th>
                    <th>RSRP</th>
                    <th>Frame</th>
                    <th>Raw Hex</th>
                </tr>
        """

        for r in rows:
            html += "<tr>" + "".join(
                f"<td><code>{'' if v is None else v}</code></td>" for v in r
            ) + "</tr>"

        html += """
            </table>
        </body>
        </html>
        """

        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(html.encode("utf-8"))


if __name__ == "__main__":
    init_db()

    tcp_server = socketserver.ThreadingTCPServer(("0.0.0.0", TCP_PORT), DC500TCPHandler)
    web_server = HTTPServer(("0.0.0.0", WEB_PORT), WebHandler)

    print(f"Listening for DC500 TCP uploads on port {TCP_PORT}")
    print(f"View dashboard at http://YOUR_SERVER_IP:{WEB_PORT}")

    Thread(target=tcp_server.serve_forever, daemon=True).start()
    web_server.serve_forever()
