#!/usr/bin/env python3

################################################################################
# SPDX-FileCopyrightText: Copyright (c) 2019-2023 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
################################################################################

import sys
import os

# deepstream_python_apps/common 모듈 쓸 수 있게 상위 폴더 추가
sys.path.append("../")

import gi
gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst

from optparse import OptionParser

from common.platform_info import PlatformInfo
from common.bus_call import bus_call
from common.utils import long_to_uint64

import pyds
import time
import yaml

# 👉 네가 만든 전낙상 로직 / 설정 타입
from src.zone_logic_simple import SimpleZoneMonitor, load_zone_config


# 👉 상태 JSON / 타임라인 CSV 저장
from src.storage import write_status

# 👉 콘솔에 ALERT 찍을 때 사용
from src.alerts import console_alert


MAX_DISPLAY_LEN = 64
MAX_TIME_STAMP_LEN = 32
PGIE_CLASS_ID_VEHICLE = 0
PGIE_CLASS_ID_BICYCLE = 1
PGIE_CLASS_ID_PERSON = 2
PGIE_CLASS_ID_ROADSIGN = 3
MUXER_OUTPUT_WIDTH = 1920
MUXER_OUTPUT_HEIGHT = 1080
MUXER_BATCH_TIMEOUT_USEC = 33000
input_file = None
schema_type = 0
proto_lib = None
conn_str = "localhost;2181;testTopic"
cfg_file = None
topic = None
no_display = False

PGIE_CONFIG_FILE = "dstest4_pgie_config.txt"
MSCONV_CONFIG_FILE = "dstest4_msgconv_config.txt"

pgie_classes_str = ["Vehicle", "TwoWheeler", "Person", "Roadsign"]

OUTPUT_STATUS_PATH = os.path.join(os.path.dirname(__file__), "output", "status.json")

def generate_vehicle_meta(data):
    obj = pyds.NvDsVehicleObject.cast(data)
    obj.type = "sedan"
    obj.color = "blue"
    obj.make = "Bugatti"
    obj.model = "M"
    obj.license = "XX1234"
    obj.region = "CA"
    return obj


def generate_person_meta(data):
    obj = pyds.NvDsPersonObject.cast(data)
    obj.age = 45
    obj.cap = "none"
    obj.hair = "black"
    obj.gender = "male"
    obj.apparel = "formal"
    return obj


def generate_event_msg_meta(data, class_id):
    meta = pyds.NvDsEventMsgMeta.cast(data)
    meta.sensorId = 0
    meta.placeId = 0
    meta.moduleId = 0
    meta.sensorStr = "sensor-0"
    meta.ts = pyds.alloc_buffer(MAX_TIME_STAMP_LEN + 1)
    pyds.generate_ts_rfc3339(meta.ts, MAX_TIME_STAMP_LEN)

    # This demonstrates how to attach custom objects.
    # Any custom object as per requirement can be generated and attached
    # like NvDsVehicleObject / NvDsPersonObject. Then that object should
    # be handled in payload generator library (nvmsgconv.cpp) accordingly.
    if class_id == PGIE_CLASS_ID_VEHICLE:
        meta.type = pyds.NvDsEventType.NVDS_EVENT_MOVING
        meta.objType = pyds.NvDsObjectType.NVDS_OBJECT_TYPE_VEHICLE
        meta.objClassId = PGIE_CLASS_ID_VEHICLE
        obj = pyds.alloc_nvds_vehicle_object()
        obj = generate_vehicle_meta(obj)
        meta.extMsg = obj
        meta.extMsgSize = sys.getsizeof(pyds.NvDsVehicleObject)
    if class_id == PGIE_CLASS_ID_PERSON:
        meta.type = pyds.NvDsEventType.NVDS_EVENT_ENTRY
        meta.objType = pyds.NvDsObjectType.NVDS_OBJECT_TYPE_PERSON
        meta.objClassId = PGIE_CLASS_ID_PERSON
        obj = pyds.alloc_nvds_person_object()
        obj = generate_person_meta(obj)
        meta.extMsg = obj
        meta.extMsgSize = sys.getsizeof(pyds.NvDsPersonObject)
    return meta


# osd_sink_pad_buffer_probe  will extract metadata received on OSD sink pad
# and update params for drawing rectangle, object information etc.
# IMPORTANT NOTE:
# a) probe() callbacks are synchronous and thus holds the buffer
#    (info.get_buffer()) from traversing the pipeline until user return.
# b) loops inside probe() callback could be costly in python.
#    So users shall optimize according to their use-case.
# osd_sink_pad_buffer_probe  will extract metadata received on OSD sink pad
# and update params for drawing rectangle, object information etc.
def osd_sink_pad_buffer_probe(pad, info, u_data):
    """
    DeepStream가 프레임마다 부르는 콜백.
    여기서:
      - 사람(person) 객체만 골라서
      - 침대 Zone1 전낙상 로직(SimpleZoneMonitor)에 넣고
      - 박스 색(초록/노랑/빨강) 바꾸고
      - status.json에 상태를 기록한다.
    """
    frame_number = 0

    # u_data: main()에서 넘긴 딕셔너리 (zone_monitor, camera_id, fps_hint, person_class_id)
    if u_data is None:
        return Gst.PadProbeReturn.OK

    zone_monitor: SimpleZoneMonitor = u_data.get("zone_monitor")
    camera_id = u_data.get("camera_id", "cam01")
    fps_hint = float(u_data.get("fps_hint", 30.0))
    person_class_id = int(u_data.get("person_class_id", PGIE_CLASS_ID_PERSON))

    # 프레임 간 시간 간격(dt)을 단순히 fps로부터 추정 (예: 30fps → 1/30초)
    dt = 1.0 / fps_hint if fps_hint > 0 else 1.0 / 30.0

    gst_buffer = info.get_buffer()
    if not gst_buffer:
        print("Unable to get GstBuffer ")
        return Gst.PadProbeReturn.OK

    # DeepStream 메타데이터(batch_meta) 가져오기
    batch_meta = pyds.gst_buffer_get_nvds_batch_meta(hash(gst_buffer))
    if not batch_meta:
        return Gst.PadProbeReturn.OK

    # 프레임들 순회
    l_frame = batch_meta.frame_meta_list
    while l_frame is not None:
        try:
            frame_meta = pyds.NvDsFrameMeta.cast(l_frame.data)
        except StopIteration:
            break

        frame_number = frame_meta.frame_num

        # 이 프레임 안의 객체들 순회
        l_obj = frame_meta.obj_meta_list
        while l_obj is not None:
            try:
                obj_meta = pyds.NvDsObjectMeta.cast(l_obj.data)
            except StopIteration:
                break

            # DeepStream이 붙여준 class_id 기준으로 "사람"만 전낙상 로직에 사용
            if obj_meta.class_id == person_class_id and zone_monitor is not None:
                rect = obj_meta.rect_params
                bbox = (rect.left, rect.top, rect.width, rect.height)

                # 👇 네가 만든 전낙상 뇌 호출 (Zone1만 사용하는 SimpleZoneMonitor)
                res = zone_monitor.update(bbox=bbox, dt=dt)

                in_zone1 = bool(res.get("in_zone1", False))
                dwell = float(res.get("dwell", 0.0))
                level = res.get("level", "SAFE")  # "SAFE" / "PREFALL_SHORT" / "PREFALL_ALERT"

                # --- 박스 스타일 바꾸기 ---
                rect.border_width = 3
                if level == "SAFE":
                    # 초록
                    rect.border_color.set(0.0, 1.0, 0.0, 1.0)
                elif level == "PREFALL_SHORT":
                    # 노랑
                    rect.border_color.set(1.0, 1.0, 0.0, 1.0)
                elif level == "PREFALL_ALERT":
                    # 빨강
                    rect.border_color.set(1.0, 0.0, 0.0, 1.0)

                # --- 화면에 표시되는 텍스트 업데이트 ---
                txt_params = obj_meta.text_params
                txt_params.display_text = f"Person | {level} {dwell:.1f}s"
                txt_params.font_params.font_name = "Serif"
                txt_params.font_params.font_size = 10
                txt_params.font_params.font_color.set(1.0, 1.0, 1.0, 1.0)
                txt_params.set_bg_clr = 1
                txt_params.text_bg_clr.set(0.0, 0.0, 0.0, 0.8)

                # --- 상태 파일(status.json)로 기록 ---
                try:
                    write_status(
                        OUTPUT_STATUS_PATH,
                        camera_id=camera_id,
                        track_id=int(obj_meta.object_id),
                        prefall=in_zone1,
                        dwell=dwell,
                    )
                except Exception as e:
                    print("write_status error:", e)

                # --- ALERT면 콘솔에도 한 번 찍어주기 ---
                if level == "PREFALL_ALERT":
                    try:
                        console_alert(camera_id, int(obj_meta.object_id), dwell)
                    except Exception as e:
                        print("console_alert error:", e)

            # 다음 객체로
            try:
                l_obj = l_obj.next
            except StopIteration:
                break

        # 다음 프레임으로
        try:
            l_frame = l_frame.next
        except StopIteration:
            break

    return Gst.PadProbeReturn.OK


def main(args):
    platform_info = PlatformInfo()
    Gst.init(None)

    # === Bedwatch Zone1 설정 불러오기 & 모니터 생성 ===
    zone_cfg_path = os.path.join(
    os.path.dirname(__file__),
    "configs",
    "zones",
    "minimal_room.yaml",
    )


    zone_cfg = load_zone_config(zone_cfg_path)
    zone_monitor = SimpleZoneMonitor(zone_cfg)

    # pad-probe에 같이 넘길 데이터 묶음
    user_data = {
        "zone_monitor": zone_monitor,
        "camera_id": getattr(zone_cfg, "camera_id", "cam01"),
        "fps_hint": getattr(zone_cfg, "fps", 30.0),
        # 사람 class_id (PeopleNet 기본 0, 모델에 따라 조정 가능)
        "person_class_id": PGIE_CLASS_ID_PERSON,
    }

    # Deprecated: following meta_copy_func and meta_free_func
    # have been moved to the binding as event_msg_meta_copy_func()
    # and event_msg_meta_release_func() respectively.
    # Hence, registering and unsetting these callbacks in not needed
    # anymore. Please extend the above functions as necessary instead.
    # # registering callbacks
    # pyds.register_user_copyfunc(meta_copy_func)
    # pyds.register_user_releasefunc(meta_free_func)

    print("Creating Pipeline \n ")

    pipeline = Gst.Pipeline()

    if not pipeline:
        sys.stderr.write(" Unable to create Pipeline \n")

    print("Creating Source \n ")
    source = Gst.ElementFactory.make("filesrc", "file-source")
    if not source:
        sys.stderr.write(" Unable to create Source \n")

    print("Creating H264Parser \n")
    h264parser = Gst.ElementFactory.make("h264parse", "h264-parser")
    if not h264parser:
        sys.stderr.write(" Unable to create h264 parser \n")

    print("Creating Decoder \n")
    decoder = Gst.ElementFactory.make("nvv4l2decoder", "nvv4l2-decoder")
    if not decoder:
        sys.stderr.write(" Unable to create Nvv4l2 Decoder \n")

    streammux = Gst.ElementFactory.make("nvstreammux", "Stream-muxer")
    if not streammux:
        sys.stderr.write(" Unable to create NvStreamMux \n")

    pgie = Gst.ElementFactory.make("nvinfer", "primary-inference")
    if not pgie:
        sys.stderr.write(" Unable to create pgie \n")

    nvvidconv = Gst.ElementFactory.make("nvvideoconvert", "convertor")
    if not nvvidconv:
        sys.stderr.write(" Unable to create nvvidconv \n")

    nvosd = Gst.ElementFactory.make("nvdsosd", "onscreendisplay")
    if not nvosd:
        sys.stderr.write(" Unable to create nvosd \n")

    msgconv = Gst.ElementFactory.make("nvmsgconv", "nvmsg-converter")
    if not msgconv:
        sys.stderr.write(" Unable to create msgconv \n")

    msgbroker = Gst.ElementFactory.make("nvmsgbroker", "nvmsg-broker")
    if not msgbroker:
        sys.stderr.write(" Unable to create msgbroker \n")

    tee = Gst.ElementFactory.make("tee", "nvsink-tee")
    if not tee:
        sys.stderr.write(" Unable to create tee \n")

    queue1 = Gst.ElementFactory.make("queue", "nvtee-que1")
    if not queue1:
        sys.stderr.write(" Unable to create queue1 \n")

    queue2 = Gst.ElementFactory.make("queue", "nvtee-que2")
    if not queue2:
        sys.stderr.write(" Unable to create queue2 \n")

    if no_display:
        print("Creating FakeSink \n")
        sink = Gst.ElementFactory.make("fakesink", "fakesink")
        if not sink:
            sys.stderr.write(" Unable to create fakesink \n")
    else:
        if platform_info.is_integrated_gpu():
            print("Creating nv3dsink \n")
            sink = Gst.ElementFactory.make("nv3dsink", "nv3d-sink")
            if not sink:
                sys.stderr.write(" Unable to create nv3dsink \n")
        else:
            if platform_info.is_platform_aarch64():
                print("Creating nv3dsink \n")
                sink = Gst.ElementFactory.make("nv3dsink", "nv3d-sink")
            else:
                print("Creating EGLSink \n")
                sink = Gst.ElementFactory.make("nveglglessink", "nvvideo-renderer")
            if not sink:
                sys.stderr.write(" Unable to create egl sink \n")

    print("Playing file %s " % input_file)
    source.set_property("location", input_file)
    streammux.set_property("width", 1920)
    streammux.set_property("height", 1080)
    streammux.set_property("batch-size", 1)
    streammux.set_property("batched-push-timeout", MUXER_BATCH_TIMEOUT_USEC)
    pgie.set_property("config-file-path", PGIE_CONFIG_FILE)
    msgconv.set_property("config", MSCONV_CONFIG_FILE)
    msgconv.set_property("payload-type", schema_type)
    msgbroker.set_property("proto-lib", proto_lib)
    msgbroker.set_property("conn-str", conn_str)
    if cfg_file is not None:
        msgbroker.set_property("config", cfg_file)
    if topic is not None:
        msgbroker.set_property("topic", topic)
    msgbroker.set_property("sync", False)

    print("Adding elements to Pipeline \n")
    pipeline.add(source)
    pipeline.add(h264parser)
    pipeline.add(decoder)
    pipeline.add(streammux)
    pipeline.add(pgie)
    pipeline.add(nvvidconv)
    pipeline.add(nvosd)
    pipeline.add(tee)
    pipeline.add(queue1)
    pipeline.add(queue2)
    pipeline.add(msgconv)
    pipeline.add(msgbroker)
    pipeline.add(sink)

    print("Linking elements in the Pipeline \n")
    source.link(h264parser)
    h264parser.link(decoder)

    sinkpad = streammux.request_pad_simple("sink_0")
    if not sinkpad:
        sys.stderr.write(" Unable to get the sink pad of streammux \n")
    srcpad = decoder.get_static_pad("src")
    if not srcpad:
        sys.stderr.write(" Unable to get source pad of decoder \n")
    srcpad.link(sinkpad)

    streammux.link(pgie)
    pgie.link(nvvidconv)
    nvvidconv.link(nvosd)
    nvosd.link(tee)
    queue1.link(msgconv)
    msgconv.link(msgbroker)
    queue2.link(sink)
    sink_pad = queue1.get_static_pad("sink")
    tee_msg_pad = tee.request_pad_simple("src_%u")
    tee_render_pad = tee.request_pad_simple("src_%u")
    if not tee_msg_pad or not tee_render_pad:
        sys.stderr.write("Unable to get request pads\n")
    tee_msg_pad.link(sink_pad)
    sink_pad = queue2.get_static_pad("sink")
    tee_render_pad.link(sink_pad)

    # create an event loop and feed gstreamer bus messages to it
    loop = GLib.MainLoop()
    bus = pipeline.get_bus()
    bus.add_signal_watch()
    bus.connect("message", bus_call, loop)

    osdsinkpad = nvosd.get_static_pad("sink")
    if not osdsinkpad:
        sys.stderr.write(" Unable to get sink pad of nvosd \n")

    # Bedwatch용 pad-probe: user_data에 zone_monitor 등 정보를 담아서 넘김
    osdsinkpad.add_probe(
        Gst.PadProbeType.BUFFER,
        osd_sink_pad_buffer_probe,
        user_data,
    )

    print("Starting pipeline \n")

    # start play back and listen to events
    pipeline.set_state(Gst.State.PLAYING)
    try:
        loop.run()
    except:
        pass

    # cleanup
    # pyds.unset_callback_funcs()
    pipeline.set_state(Gst.State.NULL)



# Parse and validate input arguments
def parse_args():
    parser = OptionParser()
    parser.add_option("-c", "--cfg-file", dest="cfg_file",
                      help="Set the adaptor config file. Optional if "
                           "connection string has relevant  details.",
                      metavar="FILE")
    parser.add_option("-i", "--input-file", dest="input_file",
                      help="Set the input H264 file", metavar="FILE")
    parser.add_option("-p", "--proto-lib", dest="proto_lib",
                      help="Absolute path of adaptor library", metavar="PATH")
    parser.add_option("", "--conn-str", dest="conn_str",
                      help="Connection string of backend server. Optional if "
                           "it is part of config file.", metavar="STR")
    parser.add_option("-s", "--schema-type", dest="schema_type", default="0",
                      help="Type of message schema (0=Full, 1=minimal), "
                           "default=0", metavar="<0|1>")
    parser.add_option("-t", "--topic", dest="topic",
                      help="Name of message topic. Optional if it is part of "
                           "connection string or config file.", metavar="TOPIC")
    parser.add_option("", "--no-display", action="store_true",
                      dest="no_display", default=False,
                      help="Disable display")

    (options, args) = parser.parse_args()

    global cfg_file
    global input_file
    global proto_lib
    global conn_str
    global topic
    global schema_type
    global no_display
    cfg_file = options.cfg_file
    input_file = options.input_file
    proto_lib = options.proto_lib
    conn_str = options.conn_str
    topic = options.topic
    no_display = options.no_display

    if not (proto_lib and input_file):
        print("Usage: python3 deepstream_test_4.py -i <H264 filename> -p "
              "<Proto adaptor library> --conn-str=<Connection string>")
        return 1

    schema_type = 0 if options.schema_type == "0" else 1


if __name__ == '__main__':
    ret = parse_args()
    # If argument parsing fails, returns failure (non-zero)
    if ret == 1:
        sys.exit(1)
    sys.exit(main(sys.argv))
