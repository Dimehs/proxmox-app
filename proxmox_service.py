import os
import time
import logging
from proxmoxer import ProxmoxAPI
from requests.exceptions import ConnectTimeout, ConnectionError
from fastapi import HTTPException

logger = logging.getLogger(__name__)

def get_px():
    """Establishes a connection to the active Proxmox node."""
    nodes = os.getenv("PVE_NODES").split(",")
    for node_ip in nodes:
        try:
            px = ProxmoxAPI(
                node_ip,
                user=os.getenv("PVE_USER"),
                token_name=os.getenv("PVE_TOKEN_NAME"),
                token_value=os.getenv("PVE_TOKEN_VALUE"),
                verify_ssl=False, timeout=3
            )
            px.nodes.get() # Test connection
            logger.info(f"Connected to Proxmox node: {node_ip}")
            return px
        except (ConnectTimeout, ConnectionError):
            logger.warning(f"Failed to connect to node: {node_ip}")
            continue
    logger.error("All Proxmox nodes are unreachable!")
    raise Exception("All Proxmox nodes are unreachable!")

def wait_for_task(px, node, upid):
    """Blocks execution until a Proxmox task (clone/stop/delete) is finished."""
    logger.info(f"Waiting for task {upid} on {node}...")
    while True:
        task = px.nodes(node).tasks(upid).status.get()
        if task['status'] == 'stopped':
            if 'exitstatus' in task and task['exitstatus'] != 'OK':
                logger.error(f"Task {upid} failed: {task['exitstatus']}")
                raise HTTPException(status_code=500, detail=f"Proxmox Task Failed: {task['exitstatus']}")
            logger.info(f"Task {upid} completed successfully.")
            return
        time.sleep(1)