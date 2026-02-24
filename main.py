from fastapi import FastAPI, Depends, HTTPException, Query
from sqlalchemy.orm import Session
import os, models, database, time
from proxmoxer import ProxmoxAPI
from requests.exceptions import ConnectTimeout, ConnectionError
from dotenv import load_dotenv

load_dotenv()
models.Base.metadata.create_all(bind=database.engine)

app = FastAPI(title="Proxmox Lab Orchestrator")

# Dynamic Connection Logic
def get_px():
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
            return px
        except (ConnectTimeout, ConnectionError):
            continue
    raise Exception("All Proxmox nodes are unreachable!")

# --- Admin Endpoints ---

@app.post("/templates/")
def add_template(name: str, vmid: int, role: str, is_ct: bool, db: Session = Depends(database.get_db)):
    temp = models.Template(name=name, pve_vmid=vmid, role=role, is_container=is_ct)
    db.add(temp)
    db.commit()
    return {"status": f"Template {name} registered"}

# --- Deployment Logic ---

@app.post("/deploy/bulk")
def deploy_bulk(start_table: int, end_table: int, db: Session = Depends(database.get_db)):
    results = []
    for i in range(start_table, end_table + 1):
        res = deploy_table(i, db)
        results.append(res)
    return {"results": results}

@app.post("/deploy/table/{table_num}")
def deploy_table(table_num: int, db: Session = Depends(database.get_db)):
    px = get_px()
    vlan = 50 + table_num
    node_list = ["proxmox1", "proxmox2", "proxmox3"]
    target_node = node_list[table_num % 3]
    gw = f"10.10.{vlan}.1"

    # Ensure Table entry exists
    table = db.query(models.TableLab).filter_by(table_number=table_num).first()
    if not table:
        table = models.TableLab(table_number=table_num, vlan_id=vlan)
        db.add(table)
        db.commit()

    # 1. Deploy Kali (.100)
    kali_temp = db.query(models.Template).filter_by(role='attacker').first()
    if kali_temp:
        new_id = px.cluster.nextid.get()
        px.nodes(target_node).qemu(kali_temp.pve_vmid).clone.post(
            newid=new_id, name=f"T{table_num}-Kali", full=1, storage="Data"
        )
        px.nodes(target_node).qemu(new_id).config.post(
            net0=f"virtio,bridge=vmbr0,tag={vlan}",
            ipconfig0=f"ip=10.10.{vlan}.100/24,gw={gw}"
        )
        db.add(models.DeployedResource(vmid=new_id, table_id=table.id, node=target_node, type='qemu'))
        px.nodes(target_node).qemu(new_id).status.start.post()

    # 2. Deploy Targets (.101, .102)
    target_temps = db.query(models.Template).filter_by(role='target').all()
    for idx, t_temp in enumerate(target_temps[:2]): # Max 2 targets
        last_octet = 101 + idx
        new_id = px.cluster.nextid.get()
        if t_temp.is_container:
            px.nodes(target_node).lxc.create.post(
                vmid=new_id, ostemplate=t_temp.name, storage="Data",
                net0=f"name=eth0,bridge=vmbr0,gw={gw},ip=10.10.{vlan}.{last_octet}/24,tag={vlan}"
            )
            db.add(models.DeployedResource(vmid=new_id, table_id=table.id, node=target_node, type='lxc'))
            px.nodes(target_node).lxc(new_id).status.start.post()
            
    db.commit()
    return {"status": f"Table {table_num} deployed on {target_node}"}

@app.delete("/table/{table_num}")
def delete_table(table_num: int, db: Session = Depends(database.get_db)):
    px = get_px()
    table = db.query(models.TableLab).filter_by(table_number=table_num).first()
    resources = db.query(models.DeployedResource).filter_by(table_id=table.id).all()
    
    for res in resources:
        if res.type == 'qemu':
            px.nodes(res.node).qemu(res.vmid).status.stop.post()
            time.sleep(2) # Wait for shutdown
            px.nodes(res.node).qemu(res.vmid).delete()
        else:
            px.nodes(res.node).lxc(res.vmid).status.stop.post()
            px.nodes(res.node).lxc(res.vmid).delete()
        db.delete(res)
    
    db.commit()
    return {"status": f"Table {table_num} purged"}
