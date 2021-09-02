# pylint: disable=protected-access

import asyncio
from datetime import datetime
import json
from pathlib import Path
from typing import Dict, Optional

from PIL import Image
import aiohttp
import typer

from ..test_util.anonymize import anonymize_data
from ..unifi_data import (
    EVENT_MOTION,
    EVENT_SMART_DETECT_ZONE,
    LIVE_RING_FROM_WEBSOCKET,
    WSJSONPacketFrame,
    WSPacket,
)
from ..unifi_protect_server import UpvServer

SLEEP_INTERVAL = 2


def placeholder_image(output_path: Path, width: int, height: Optional[int] = None):
    if height is None:
        height = width

    image = Image.new("RGB", (width, height), (128, 128, 128))
    image.save(output_path, "PNG")


class SampleDataGenerator:
    """Generate sample data for debugging and testing purposes"""

    _record_num_ws: int = 0
    _record_ws_start_time: Optional[datetime] = None
    _record_listen_for_events: bool = False
    _record_ws_messages: Dict[str, dict] = {}

    client: UpvServer
    output_folder: Path
    anonymize: bool
    wait_time: int

    def __init__(self, client: UpvServer, output: Path, anonymize: bool, wait_time: int):
        self.client = client
        self.output_folder = output
        self.anonymize = anonymize
        self.wait_time = wait_time

    def generate(self):
        loop = asyncio.get_event_loop()
        loop.run_until_complete(self.async_generate())

    async def async_generate(self, close_session=True):
        self.output_folder.mkdir(parents=True, exist_ok=True)
        self.client.ws_callback = self._handle_ws_message

        typer.echo("Updating devices...")
        await self.client.update(True)

        data = await self.client.api_request("bootstrap")
        self.write_json_file("sample_boostrap", data)

        data = await self.client.api_request("liveviews")
        self.write_json_file("sample_liveviews", data)

        await self.record_ws_events()
        await self.generate_event_data()
        await self.generate_device_data()

        if close_session:
            await self.client.req.close()

    async def record_ws_events(self):
        if self.wait_time <= 0:
            typer.echo("Skipping recording Websocket messages...")
            return

        self._record_num_ws = 0
        self._record_ws_start_time = datetime.now()
        self._record_listen_for_events = True
        self._record_ws_messages = {}

        with typer.progressbar(range(self.wait_time // SLEEP_INTERVAL), label="Waiting for WS messages") as progress:
            for i in progress:
                if i > 0:
                    await asyncio.sleep(SLEEP_INTERVAL)

        self._record_listen_for_events = False
        await self.client.async_disconnect_ws()
        self.write_json_file("sample_ws_messages", self._record_ws_messages, anonymize=False)

    def write_json_file(self, name: str, data: dict, anonymize: Optional[bool] = None):
        if anonymize is None:
            anonymize = self.anonymize

        if anonymize:
            data = anonymize_data(data)

        typer.echo(f"Writing {name}...")
        with open(self.output_folder / f"{name}.json", "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4)
            f.write("\n")

    def write_image_file(self, name: str, raw: bytes):
        typer.echo(f"Writing {name}...")
        with open(self.output_folder / f"{name}.png", "wb") as f:
            f.write(raw)

    async def generate_event_data(self):
        data = await self.client.get_raw_events()
        heatmap_event = None
        for event in data:
            if event.get("heatmap") is not None and event.get("type") in (EVENT_MOTION, EVENT_SMART_DETECT_ZONE):
                heatmap_event = event

        if heatmap_event is not None:
            self.client._process_events([heatmap_event], LIVE_RING_FROM_WEBSOCKET)

        self.write_json_file("sample_raw_events", data)

    async def generate_device_data(self):
        has_heatmap = False
        is_camera_online = False
        camera_id: Optional[str] = None

        is_light_online = False
        light_id: Optional[str] = None

        is_viewport_online = False
        viewport_id: Optional[str] = None

        for key, item in self.client.devices.items():
            if item["type"] in ("camera", "doorbell") and not has_heatmap:
                use_camera = False
                # prefer cameras with a heatmap first
                if item["event_heatmap"] is not None:
                    use_camera = True
                    has_heatmap = True
                # then prefer cameras that are online
                elif not is_camera_online:
                    use_camera = True
                    if item["online"]:
                        is_camera_online = True

                if use_camera:
                    camera_id = key
            elif item["type"] == "light" and not is_light_online:
                light_id = key
                if item["online"]:
                    is_light_online = True
            elif item["type"] == "viewer" and not is_viewport_online:
                viewport_id = key
                if item["online"]:
                    is_viewport_online = True

        if camera_id is None:
            typer.echo("No camera found. Skipping camera endpoints...")
        else:
            await self.generate_camera_data(camera_id)

        if light_id is None:
            typer.echo("No light found. Skipping light endpoints...")
        else:
            await self.generate_light_data(light_id)

        if viewport_id is None:
            typer.echo("No viewport found. Skipping viewport endpoints...")
        else:
            await self.generate_viewport_data(viewport_id)

    async def generate_camera_data(self, camera_id: str):
        filename = "sample_camera_thumbnail"
        if self.anonymize:
            typer.echo(f"Writing {filename}...")
            placeholder_image(self.output_folder / f"{filename}.png", 640)
        else:
            self.write_image_file(filename, await self.client.get_thumbnail(camera_id=camera_id))

        if self.client.devices[camera_id]["event_heatmap"] is None:
            typer.echo("Camera has no heatmap, skipping heatmap generation...")
        else:
            self.write_image_file("sample_camera_heatmap", await self.client.get_heatmap(camera_id=camera_id))

        filename = "sample_camera_snapshot"
        if self.anonymize:
            typer.echo(f"Writing {filename}...")
            placeholder_image(self.output_folder / f"{filename}.png", 1920, 1080)
        else:
            self.write_image_file(filename, await self.client.get_snapshot_image(camera_id=camera_id))

        data = await self.client._get_camera_detail(camera_id=camera_id)
        self.write_json_file("sample_camera", data)

    async def generate_light_data(self, light_id: str):
        data = await self.client._get_light_detail(light_id=light_id)
        self.write_json_file("sample_light", data)

    async def generate_viewport_data(self, viewport_id: str):
        data = await self.client._get_viewport_detail(viewport_id=viewport_id)
        self.write_json_file("sample_viewport", data)

    def _handle_ws_message(self, msg: aiohttp.WSMessage):
        if not self._record_listen_for_events:
            return

        now = datetime.now()
        self._record_num_ws += 1
        time_offset = (now - self._record_ws_start_time).total_seconds()

        if msg.type == aiohttp.WSMsgType.BINARY:
            packet = WSPacket(msg.data)

            if not isinstance(packet.action_frame, WSJSONPacketFrame):
                typer.secho(f"Got non-JSON action frame: {packet.action_frame.payload_format}", fg="yellow")
                return

            if not isinstance(packet.data_frame, WSJSONPacketFrame):
                typer.secho(f"Got non-JSON data frame: {packet.data_frame.payload_format}", fg="yellow")
                return

            if self.anonymize:
                packet.action_frame.data = anonymize_data(packet.action_frame.data)
                packet.data_frame.data = anonymize_data(packet.data_frame.data)

            self._record_ws_messages[time_offset] = {
                "raw": packet.raw_base64,
                "action": packet.action_frame.data,
                "data": packet.data_frame.data,
            }
        else:
            typer.secho(f"Got non-binary message: {msg.type}", fg="yellow")