import logging
import paho.mqtt.client as mqtt
from database import SessionLocal, Printer, PrinterStatus

logger = logging.getLogger(__name__)

# The farm MQTT broker is where the ESP8266 connects.
# This might be different from the Bambu built-in brokers.
FARM_MQTT_BROKER = "localhost"
FARM_MQTT_PORT = 1883

class FarmMQTTClient:
    def __init__(self):
        self.client = mqtt.Client()
        self.client.on_connect = self.on_connect
        self.client.on_message = self.on_message

    def start(self):
        try:
            self.client.connect(FARM_MQTT_BROKER, FARM_MQTT_PORT, 60)
            self.client.loop_start()
            logger.info(f"Connected to Farm MQTT Broker at {FARM_MQTT_BROKER}")
        except Exception as e:
            logger.error(f"Failed to connect to Farm MQTT Broker: {e}")

    def stop(self):
        self.client.loop_stop()
        self.client.disconnect()

    def on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            # Subscribe to all printer buttons: farm/printer_id/button
            self.client.subscribe("farm/+/button")
            logger.info("Subscribed to farm/+/button")
        else:
            logger.error(f"Farm MQTT Connection failed with code {rc}")

    def on_message(self, client, userdata, msg):
        # Topic format: farm/{printer_name}/button
        try:
            topic_parts = msg.topic.split('/')
            if len(topic_parts) == 3 and topic_parts[2] == "button":
                printer_name = topic_parts[1]
                payload = msg.payload.decode("utf-8").strip()
                
                if payload == "PRESSED":
                    self.handle_button_press(printer_name)
        except Exception as e:
            logger.error(f"Error handling farm MQTT message: {e}")

    def handle_button_press(self, printer_name: str):
        db = SessionLocal()
        printer = db.query(Printer).filter(Printer.name == printer_name).first()
        
        if not printer:
            logger.warning(f"Button pressed for unknown printer: {printer_name}")
            db.close()
            return
            
        if printer.status == PrinterStatus.WAITING_CLEAN:
            logger.info(f"Printer {printer_name} cleaned! Setting status to READY.")
            printer.status = PrinterStatus.READY
            db.commit()
        else:
            logger.info(f"Button pressed for {printer_name}, but status is {printer.status}. Ignored.")
            
        db.close()

farm_mqtt_client = FarmMQTTClient()
