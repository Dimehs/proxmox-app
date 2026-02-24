from fastapi import FastAPI, Depends, HTTPException, status, Request
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
import models, database, auth_models, security, proxmox_service
from jose import JWTError, jwt
from dotenv import load_dotenv
from datetime import timedelta
import logging
import sys

load_dotenv()

# Setup Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("orchestrator.log"),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# Initialize DB tables (Lab models + Auth models)
models.Base.metadata.create_all(bind=database.engine)
auth_models.Base.metadata.create_all(bind=database.engine)

app = FastAPI(title="Proxmox Lab Orchestrator")

# Setup Templates
templates = Jinja2Templates(directory=".")

# --- Security & Auth ---
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")

def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(database.get_db)):
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, security.SECRET_KEY, algorithms=[security.ALGORITHM])
        username: str = payload.get("sub")
        if username is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception
    user = db.query(auth_models.User).filter(auth_models.User.username == username).first()
    if user is None:
        raise credentials_exception
    return user

@app.post("/token")
def login_for_access_token(form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(database.get_db)):
    user = db.query(auth_models.User).filter(auth_models.User.username == form_data.username).first()
    if not user or not security.verify_password(form_data.password, user.hashed_password):
        logger.warning(f"Failed login attempt for user: {form_data.username}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    logger.info(f"User logged in: {form_data.username}")
    access_token_expires = timedelta(minutes=security.ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = security.create_access_token(
        data={"sub": user.username}, expires_delta=access_token_expires
    )
    return {"access_token": access_token, "token_type": "bearer"}

# --- Frontend Routes ---

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})

@app.get("/", response_class=HTMLResponse)
def dashboard_page(request: Request):
    return templates.TemplateResponse("dashboard.html", {"request": request})

# --- API Data Endpoints (For UI) ---

@app.get("/api/dashboard")
def get_dashboard_data(db: Session = Depends(database.get_db), current_user: auth_models.User = Depends(get_current_user)):
    templates_list = db.query(models.Template).all()
    tables = db.query(models.TableLab).all()
    resources = db.query(models.DeployedResource).all()
    return {"templates": templates_list, "tables": tables, "resources": resources}

# --- Admin Endpoints ---

@app.post("/templates/")
def add_template(name: str, vmid: int, role: str, is_ct: bool, 
                 db: Session = Depends(database.get_db), 
                 current_user: auth_models.User = Depends(get_current_user)):
    temp = models.Template(name=name, pve_vmid=vmid, role=role, is_container=is_ct)
    db.add(temp)
    db.commit()
    logger.info(f"Template registered: {name} (VMID: {vmid}) by {current_user.username}")
    return {"status": f"Template {name} registered"}

# --- Deployment Logic ---

@app.post("/deploy/bulk")
def deploy_bulk(start_table: int, end_table: int, 
                db: Session = Depends(database.get_db),
                current_user: auth_models.User = Depends(get_current_user)):
    results = []
    for i in range(start_table, end_table + 1):
        res = deploy_table(i, db, current_user)
        results.append(res)
    return {"results": results}

@app.post("/deploy/table/{table_num}")
def deploy_table(table_num: int, 
                 db: Session = Depends(database.get_db),
                 current_user: auth_models.User = Depends(get_current_user)):
    logger.info(f"Starting deployment for Table {table_num} requested by {current_user.username}")
    px = proxmox_service.get_px()
    vlan = 50 + table_num
    # Fetch actual nodes from cluster and sort to ensure consistent modulo placement
    node_list = sorted([n['node'] for n in px.nodes.get() if n['status'] == 'online'])
    target_node = node_list[table_num % len(node_list)]
    gw = f"10.10.{vlan}.1"

    # Ensure Table entry exists
    table = db.query(models.TableLab).filter_by(table_number=table_num).first()
    if not table:
        table = models.TableLab(table_number=table_num, vlan_id=vlan)
        db.add(table)
        db.commit()
        db.refresh(table)

    # 1. Deploy Kali (.100)
    kali_temp = db.query(models.Template).filter_by(role='attacker').first()
    if kali_temp:
        new_id = px.cluster.nextid.get()
        upid = px.nodes(target_node).qemu(kali_temp.pve_vmid).clone.post(
            newid=new_id, name=f"T{table_num}-Kali", full=1, storage="Data"
        )
        proxmox_service.wait_for_task(px, target_node, upid)
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
            upid = px.nodes(target_node).lxc(t_temp.pve_vmid).clone.post(
                newid=new_id, hostname=f"T{table_num}-{t_temp.name}", full=1, storage="Data"
            )
            proxmox_service.wait_for_task(px, target_node, upid)
            px.nodes(target_node).lxc(new_id).config.post(
                net0=f"name=eth0,bridge=vmbr0,gw={gw},ip=10.10.{vlan}.{last_octet}/24,tag={vlan}"
            )
            db.add(models.DeployedResource(vmid=new_id, table_id=table.id, node=target_node, type='lxc'))
            px.nodes(target_node).lxc(new_id).status.start.post()
            
    db.commit()
    logger.info(f"Table {table_num} deployed successfully on {target_node}")
    return {"status": f"Table {table_num} deployed on {target_node}"}

@app.delete("/table/{table_num}")
def delete_table(table_num: int, 
                 db: Session = Depends(database.get_db),
                 current_user: auth_models.User = Depends(get_current_user)):
    logger.info(f"Deletion requested for Table {table_num} by {current_user.username}")
    px = proxmox_service.get_px()
    table = db.query(models.TableLab).filter_by(table_number=table_num).first()
    resources = db.query(models.DeployedResource).filter_by(table_id=table.id).all()
    
    for res in resources:
        if res.type == 'qemu':
            upid = px.nodes(res.node).qemu(res.vmid).status.stop.post()
            proxmox_service.wait_for_task(px, res.node, upid)
            upid = px.nodes(res.node).qemu(res.vmid).delete()
            proxmox_service.wait_for_task(px, res.node, upid)
        else:
            upid = px.nodes(res.node).lxc(res.vmid).status.stop.post()
            proxmox_service.wait_for_task(px, res.node, upid)
            upid = px.nodes(res.node).lxc(res.vmid).delete()
            proxmox_service.wait_for_task(px, res.node, upid)
        db.delete(res)
    
    db.commit()
    logger.info(f"Table {table_num} purged successfully")
    return {"status": f"Table {table_num} purged"}

@app.on_event("startup")
def create_default_admin():
    db = database.SessionLocal()
    if not db.query(auth_models.User).filter_by(username="admin").first():
        admin = auth_models.User(username="admin", hashed_password=security.get_password_hash("P@sw0rd"))
        db.add(admin)
        db.commit()
    db.close()
