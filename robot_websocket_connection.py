#!/usr/bin/env python3

# ROS Imports
import time
import rclpy
from rclpy.node import Node

# ROS Custom Import
from robot_msgs_r2.srv import ProcessTrigger


# Python Imports
import os
import json
import asyncio
import signal
from subprocess import *
from threading import Thread, Event

# PyYaml Import
import yaml

# Websockets Import
import websockets

# ZMQ Import
import zmq


event = Event()
base_url = "ws://localhost:2323/ws"  # Changed the connection to localhost.


class RobotWebsocketConnection(Node):
    def __init__(self, base_url):
        super().__init__("robot_websocket_connection")

        self.process_launch_statuses = {
            "webtty": False,
        }

        self.process_run_statuses = {
            "webtty": False
        }

        self.machine_id = None
        self.process = "webtty_v1.1"  # Webtty Process Name
        # NOTE - need to update the app.ts for this as soon as the connection breaks it should make a api call telling the connection is broken and need to restart.
        # and also when the session is terminated we need to make the websocket coonection live not forcefully close it. - have to fix it.

        # NOTE - need to remove the client token print statement from the app.ts

        self.host_token = None
        self.client_token = None
        self.base_url = base_url
        self.zmq_url = "tcp://localhost:5555"
        self.host_token_sent = False
        self.stop_thread = False
        self.get_logger().info("Initialised the Robot WebSocketConnection!!!!")
        self.get_machine_id()  # Get the machine Id from the local.

        self.process_handler_service = self.create_service(
            ProcessTrigger, f"{self.machine_id}/ppmt_process_handler", self.process_handler_cb
        )

        self.websocket_connect = Thread(target=self.run)
        self.websocket_connect.start()

    def run(self):
        asyncio.run(self.websocket_connection())

    def process_handler_cb(self, request, response):
        process_name = request.process_name
        process_state = request.state

        if process_name in self.process_launch_statuses.keys():
            self.process_launch_statuses[process_name] = process_state
            response.success = True
            if process_state:
                response.message = "Starting " + process_name + "!"
            else:
                response.message = "Stopping " + process_name + "!"

        else:
            response.success = False
            response.message = "Process does not exist"
        return response

    def get_machine_id(self):
        """Get the Machine Id from the robot local storage.
        """
        machine_info_path = os.path.join(
            os.getenv("HOME"), ".machineinfo.yaml")
        try:
            with open(machine_info_path, "r") as file:
                data = yaml.safe_load(file)
                self.machine_id = data.get('MACHINEID', 'Unknown')
        except FileNotFoundError:
            self.get_logger().info(
                f"The file {machine_info_path} does not exist.")
        except Exception as e:
            self.get_logger().error(
                f"Exception while reading the file - {str(e)}")

    async def websocket_connection(self):
        """
        Initialising the Websocket Connection.
        """

        async with websockets.connect(self.base_url) as websocket:
            zmq_thread = Thread(target=self.zmq_client)
            zmq_thread.start()

            await websocket.send(json.dumps({"connection_type": 1, "machine_id": self.machine_id}))
            while True:
                try:
                    response = await websocket.recv()
                    json_response_server = json.loads(response)
                    self.get_logger().info(response)

                    if 'client_token' in json_response_server:
                        self.client_token = json_response_server['client_token']
                        self.get_logger().info("Client Token Received from the Server")

                    event.wait()
                    if not self.host_token_sent:
                        await self.sent_data_to_server(websocket=websocket)

                    time.sleep(0.01)

                except websockets.ConnectionClosedError as e:
                    self.get_logger().error(str(e))
                    break  # Exit loop on connection error

    async def sent_data_to_server(self, websocket):
        """
        Sent the Host Token to Backend Server
        """
        try:
            await websocket.send(json.dumps({"connection_type": 1,  "machine_id": self.machine_id, "host_token": self.host_token}))
            self.host_token_sent = True
            self.host_token = None  # Stopping the Reconnection
        except Exception as e:
            self.get_logger().error(
                f"Exception while sending the data to server - {str(e)}")

    def webtty_start(self):
        try:
            webtty_process = Popen(
                self.process.split(), stdout=PIPE, stderr=PIPE)  # This Process is running in the Background
            self.webtty_process = webtty_process.pid  # Keep Track of the Webtty Process
            self.get_logger().info("Started the WebTTY Process")
            self.process_run_statuses["webtty"] = True

        except Exception as e:
            self.get_logger().error(str(e))

    def webtty_stop(self):
        try:
            if self.webtty_process:  # Check if Process exist or not
                os.kill(self.webtty_process, signal.SIGKILL)
                self.get_logger().info("Stopped the WebTTY Process")
                self.process_run_statuses["webtty"] = False
                self.client_token = None  # Stopping from the Reconnection
                self.webtty_process = None  # Reset the process ID after killing
            else:
                self.get_logger().warning("No WebTTY process running.")

        except Exception as e:
            self.get_logger().error(str(e))

    def zmq_client(self):
        """
        ZMQ Client function to communicate with the Webtty-ZMQ server
        """
        context = zmq.Context()
        zmq_socket = context.socket(zmq.PAIR)
        zmq_socket.connect(self.zmq_url)
        self.get_logger().info("Waiting for the ZMQ Server to connect.......")
        try:
            while True:
                response = zmq_socket.recv_string()
                self.host_token = response
                event.set()
                while True:
                    if self.client_token is not None:
                        zmq_socket.send_string(json.dumps(
                            {"Client_Token": self.client_token}))
                        break
                time.sleep(0.01)
        except Exception as e:
            self.get_logger().error(str(e))


def spin_thread_func(node):
    try:
        while not node.stop_thread:
            rclpy.spin_once(node)
            time.sleep(0.01)
    except Exception as e:
        node.get_logger().info("Error while spinning the node!! {}".format(e))

    node.get_logger().info("\nSpin thread stopped!\n")


def main():
    rclpy.init()
    robot_websocket_connection = RobotWebsocketConnection(base_url=base_url)
    spin_thread = Thread(
        target=spin_thread_func, args=(
            robot_websocket_connection,), daemon=True
    )
    spin_thread.start()

    try:
        while rclpy.ok():
            ############### webTTY ###############
            if (
                robot_websocket_connection.process_launch_statuses["webtty"]
                and not robot_websocket_connection.process_run_statuses["webtty"]
            ):
                robot_websocket_connection.webtty_start()
                # robot_websocket_connection.process_start_functions["webtty"]
            elif (
                not robot_websocket_connection.process_launch_statuses["webtty"]
                and robot_websocket_connection.process_run_statuses["webtty"]
            ):
                robot_websocket_connection.webtty_stop()
            time.sleep(0.01)
        robot_websocket_connection.stop_thread = True
    except Exception as e:
        robot_websocket_connection.get_logger().error(str(e))
        robot_websocket_connection.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
