#!/usr/bin/env python3
import asyncio, logging, signal, sys
from pathlib import Path
import yaml
from camera_manager import CameraManager
from ai_analyzer import AIAnalyzer
from zigbee_sensors import ZigbeeSensorManager
from notifier import Notifier
from dashboard import Dashboard
from alarm import AlarmManager
from storage_manager import StorageManager
from event_store import EventStore
from chat import ChatEngine
from known_entities import KnownEntities

def load_config(path="config.yaml"):
    with open(path) as f:
        return yaml.safe_load(f)

def setup_logging(cfg):
    level = getattr(logging, cfg.get("level", "INFO").upper(), logging.INFO)
    logging.basicConfig(level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout),
                  logging.FileHandler(cfg.get("file", "monitor.log"))])

async def main():
    config = load_config()
    setup_logging(config.get("logging", {}))
    log = logging.getLogger("main")
    log.info("Starting Home AI Monitor")
    Path(config["recording"]["output_dir"]).mkdir(parents=True, exist_ok=True)
    Path(config["face_recognition"]["known_faces_dir"]).mkdir(parents=True, exist_ok=True)
    Path("known_entities").mkdir(exist_ok=True)
    stop_event = asyncio.Event()
    def _shutdown(sig, frame):
        log.info(f"Received {signal.Signals(sig).name}, shutting down...")
        stop_event.set()
    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)
    notifier = Notifier(config)
    known_entities = KnownEntities(config)
    ai_analyzer = AIAnalyzer(config, notifier, known_entities=known_entities)
    alarm_manager = AlarmManager(config, notifier)
    storage_manager = StorageManager(config, notifier)
    event_store = EventStore(config)
    chat_engine = ChatEngine(config, event_store, alarm_manager=alarm_manager,
                             config_path="config.yaml", known_entities=known_entities)
    camera_manager = CameraManager(config, ai_analyzer, notifier, alarm_manager, event_store)
    zigbee_manager = ZigbeeSensorManager(config, notifier, alarm_manager, event_store)
    alarm_manager.set_camera_manager(camera_manager)
    dashboard = Dashboard(config, camera_manager, zigbee_manager, alarm_manager, storage_manager, chat_engine)
    tasks = [
        asyncio.create_task(camera_manager.run(stop_event), name="cameras"),
        asyncio.create_task(zigbee_manager.run(stop_event), name="zigbee"),
        asyncio.create_task(dashboard.run(stop_event), name="dashboard"),
        asyncio.create_task(alarm_manager.run(stop_event), name="alarm"),
        asyncio.create_task(storage_manager.run(stop_event), name="storage"),
        asyncio.create_task(event_store.run(stop_event), name="event_store"),
    ]
    log.info("All subsystems started. Dashboard at http://localhost:%d", config["dashboard"]["port"])
    await stop_event.wait()
    log.info("Cancelling tasks...")
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    log.info("Shutdown complete.")

if __name__ == "__main__":
    asyncio.run(main())
