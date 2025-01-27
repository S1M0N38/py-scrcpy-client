import abc
from pathlib import Path
import socket
import struct
import threading
import time
from time import sleep
from typing import Any, Callable, Optional, Tuple, Union

import numpy as np
import numpy.typing as npt

from adbutils import AdbDevice, AdbError, Network, _AdbStreamConnection, adb
from av.codec import CodecContext

from .const import EVENT_FRAME, EVENT_INIT, EVENT_CHANGE, LOCK_SCREEN_ORIENTATION_UNLOCKED
from .control import ControlSender

Frame = npt.NDArray[np.int8]

VERSION = "1.20"
HERE = Path(__file__).resolve().parent
JAR = HERE / f"scrcpy-server.jar"


class Client:
    def __init__(
        self,
        device: Optional[Union[AdbDevice, str]] = None,
        max_size: int = 0,
        bitrate: int = 8000000,
        max_fps: int = 0,
        block_frame: bool = False,
        stay_awake: bool = False,
        lock_screen_orientation: int = LOCK_SCREEN_ORIENTATION_UNLOCKED,
        change_treshold: int = 100,
    ):
        """
        Create a scrcpy client.
        This client won't be started until you call .start()

        Args:
            device: Android device to coennect to. Colud be also specify by
                serial string. In device is None the client try to connect
                to the first available device in adb deamon.
            max_size: Specify the maximum dimension of the video stream. This
                dimensioin refer both to width and hight.
            bitrate: Biirate of the video stream.
            max_fps: Maximum FPS (Frame Per Second) of the video stream. If it
                is set to 0 it means that there is not limit to FPS.
                This feature is supported by android 10 or newer.
            block_frame: If set to true, the on_frame callbacks will be only
                apply on not empty frames. Otherwise try to apply on_frame
                callbacks on every frame, but this could raise exceptions in
                callbacks if they are not able to handle None value for frame.
            stay_awake: keep Android device awake while the client-server
                connection is alive.
            lock_screen_orientation: lock screen in a particular orientation.
                The available screen orientation are specify in const.py
                in variables LOCK_SCREEN_ORIENTATION*
            change_treshold: Two consecutive frames are considered different
                if the mean of the pixelwise difference is greater than 
                change_treshold. If that is the case the on_change callbacks 
                are run on the new frame. This theshold may vary from case to 
                case, so it up to you to find the best value for your 
                circumstance.
        """

        if device is None:
            device = adb.device_list()[0]
        elif isinstance(device, str):
            device = adb.device(serial=device)

        self.device = device
        self.listeners = dict(frame=[], init=[], change=[])
        self.change_threshold = change_treshold

        # User accessible
        self.last_frame: Optional[np.ndarray] = None
        self.resolution: Optional[Tuple[int, int]] = None
        self.device_name: Optional[str] = None
        self.control = ControlSender(self)

        # Params
        self.max_size = max_size
        self.bitrate = bitrate
        self.max_fps = max_fps
        self.block_frame = block_frame
        self.stay_awake = stay_awake
        self.lock_screen_orientation = lock_screen_orientation

        # Need to destroy
        self.alive = False
        self.__server_stream: Optional[_AdbStreamConnection] = None
        self.__video_socket: Optional[socket.socket] = None
        self.control_socket: Optional[socket.socket] = None
        self.control_socket_lock = threading.Lock()

    def __init_server_connection(self) -> None:
        """
        Connect to android server, there will be two sockets: video and control
        socket. This method will also set resolution property.
        """
        for _ in range(30):
            try:
                self.__video_socket = self.device.create_connection(
                    Network.LOCAL_ABSTRACT, "scrcpy"
                )
                break
            except AdbError:
                sleep(0.1)
                pass
        else:
            raise ConnectionError(
                "Failed to connect to scrcpy-server after 3 seconds."
            )

        dummy_byte = self.__video_socket.recv(1)
        if not len(dummy_byte):
            raise ConnectionError("Did not receive Dummy Byte!")

        self.control_socket = self.device.create_connection(
            Network.LOCAL_ABSTRACT, "scrcpy"
        )
        self.device_name = (
            self.__video_socket.recv(64).decode("utf-8").rstrip("\x00")
        )
        if not len(self.device_name):
            raise ConnectionError("Did not receive Device Name!")

        res = self.__video_socket.recv(4)
        self.resolution = struct.unpack(">HH", res)
        self.__video_socket.setblocking(False)

    def __deploy_server(self) -> None:
        """
        Deploy server to android device.
        Push the scrcpy-server.jar into the Android device using
        the adb.push(...). Then a basic connection between client and server
        is established.
        """
        cmd = [
            "CLASSPATH=/data/local/tmp/scrcpy-server.jar",
            "app_process",
            "/",
            "com.genymobile.scrcpy.Server",
            VERSION,  # Scrcpy server version
            "info",  # Log level: info, verbose...
            f"{self.max_size}",  # Max screen width (long side)
            f"{self.bitrate}",  # Bitrate of video
            f"{self.max_fps}",  # Max frame per second
            f"{self.lock_screen_orientation}",  # Lock screen orientation
            "true",  # Tunnel forward
            "-",  # Crop screen
            "false",  # Send frame rate to client
            "true",  # Control enabled
            "0",  # Display id
            "false",  # Show touches
            "true" if self.stay_awake else "false",  # Stay awake
            "-",  # Codec (video encoding) options
            "-",  # Encoder name
            "false",  # Power off screen after server closed
        ]
        self.device.push(JAR, "/data/local/tmp/")
        self.__server_stream = self.device.shell(cmd, stream=True)

    def start(self, threaded: bool = False) -> None:
        """
        Start the client-server connection.
        In order to avoid unpredictable behaviors, this method must be called
        after the on_init and on_frame callback are specify.

        Args:
            threaded: If set to True the stream loop willl run in a separated
                thread. This mean that the code after client.strart() will be
                run. Otherwise the client.start() method starts a endless loop
                and the code after this method will never run.
        """
        assert self.alive is False

        self.__deploy_server()
        self.__init_server_connection()
        self.alive = True
        for func in self.listeners[EVENT_INIT]:
            func(self)

        if threaded:
            threading.Thread(target=self.__stream_loop).start()
        else:
            self.__stream_loop()

    def stop(self) -> None:
        """
        Close the various socket connection.
        Stop listening (both threaded and blocked)
        """
        self.alive = False
        if self.__server_stream is not None:
            self.__server_stream.close()
        if self.control_socket is not None:
            self.control_socket.close()
        if self.__video_socket is not None:
            self.__video_socket.close()

    def __stream_loop(self) -> None:
        """
        Core loop for video parsing.
        While the connection is open (self.alive == True) recive raw h264 video
        stream and decode it into frames. These frame are those passed to
        on_frame callbacks.
        """
        codec = CodecContext.create("h264", "r")
        while self.alive:
            try:
                raw = self.__video_socket.recv(0x10000)
                for packet in codec.parse(raw):
                    for frame in codec.decode(packet):
                        frame = frame.to_ndarray(format="bgr24")
                        self.last_frame = frame
                        self.resolution = (frame.shape[1], frame.shape[0])
                        for func in self.listeners[EVENT_FRAME]:
                            func(self, frame)
            except BlockingIOError:
                time.sleep(0.01)
                if not self.block_frame:
                    for func in self.listeners[EVENT_FRAME]:
                        func(self, None)
            except OSError as e:
                if self.alive:
                    raise e

    def on_init(self, func: Callable[[Any], None]) -> None:
        """
        Add funtion to on_init listeners.
        Your function is run after client.start() is called.

        Args:
            func: callback to be called after the server starts.

        Returns:
            The list of on-init callbacks.

        """
        self.listeners[EVENT_INIT].append(func)
        return self.listeners[EVENT_INIT]

    def on_frame(self, func: Callable[[Any, Frame], None]):
        """
        Add functoin to on-frame listeners.
        Your function will be run on every valid frame recived.

        Args:
            func: callback to be called on every frame.

        Returns:
            The list of on-frame callbacks.
        """
        self.listeners[EVENT_FRAME].append(func)
        return self.listeners[EVENT_FRAME]

    def on_change(self, func: Callable[[Any, Frame], None]):
        """
        Add functoin to on-frame listeners.
        Your function when the pixel vaule of the frame are different from the 
        previous one.

        Args:
            func: callback to be called on every screen change.

        Returns:
            The list of on-frame callbacks.
        """
        self.listeners[EVENT_CHANGE].append(func)
        return self.listeners[EVENT_CHANGE]
