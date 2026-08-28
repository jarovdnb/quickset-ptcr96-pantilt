import socket
import json
import os

PANTILT_SOCKET_PATH = "/tmp/pantilt_socket.sock"

# Unix domain sockets aren't available on every platform (e.g. this Windows
# Python build) -- fall back to TCP loopback on a fixed port per socket path
# so the same client/server code works there too.
TCP_FALLBACK_PORTS = {
    PANTILT_SOCKET_PATH: 8902,
}


def has_af_unix():
    return hasattr(socket, "AF_UNIX")


def connect_local_socket(socket_path):
    if has_af_unix():
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(socket_path)
        return client
    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    client.connect(("127.0.0.1", TCP_FALLBACK_PORTS[socket_path]))
    return client


def bind_local_socket(socket_path):
    if has_af_unix():
        if os.path.exists(socket_path):
            os.remove(socket_path)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(socket_path)
        return server
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", TCP_FALLBACK_PORTS[socket_path]))
    return server


def close_local_socket(server, socket_path):
    server.close()
    if has_af_unix() and os.path.exists(socket_path):
        os.remove(socket_path)


def get_local_socket_info(socket_path=PANTILT_SOCKET_PATH):
    countdown_dict = {}

    try:
        client = connect_local_socket(socket_path)

        response = client.recv(1024)
        client.close()

        if response:
            try:
                countdown_dict = json.loads(response.decode())
                print("Received:", countdown_dict)
            except json.JSONDecodeError as e:
                print(f"JSON decode error: {e}")
        else:
            print("No data received")

    except FileNotFoundError:
        print("Socket does not exist. Is the server running?")
    except ConnectionRefusedError:
        print("Connection refused. Check if the server is active.")
    except Exception as e:
        print(f"Unexpected error: {e}")

    return countdown_dict

def get_pantilt_socket_info():
    return get_local_socket_info(PANTILT_SOCKET_PATH)
