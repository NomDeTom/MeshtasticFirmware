import threading


IFACE_CLOSE_TIMEOUT_SECONDS = 5


class InterfaceCloseTimeout(TimeoutError):
    """Raised when a Meshtastic TCP interface does not finish closing."""


def close_interface(iface, timeout=IFACE_CLOSE_TIMEOUT_SECONDS):
    """Close a Meshtastic interface without letting its reader thread hang forever.

    Native meshtasticd setups can block inside `TCPInterface.close()` while the
    Python Meshtastic client waits for its RX thread to exit. Closing in a daemon
    helper thread keeps simulator startup/shutdown responsive and lets reconnect
    continue with a fresh interface.
    """
    if iface is None:
        return

    errors = []

    def run_close():
        try:
            iface.close()
        except Exception as ex:
            errors.append(ex)

    close_thread = threading.Thread(target=run_close, daemon=True)
    close_thread.start()
    close_thread.join(timeout)
    if close_thread.is_alive():
        raise InterfaceCloseTimeout(f"interface close did not finish within {timeout:g}s")
    if errors:
        raise errors[0]
