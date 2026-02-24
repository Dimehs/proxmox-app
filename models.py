from sqlalchemy import Column, Integer, String, Boolean, ForeignKey
from database import Base

class Template(Base):
    __tablename__ = "templates"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True)
    pve_vmid = Column(Integer)  # Template ID in Proxmox
    role = Column(String)       # 'attacker' or 'target'
    is_container = Column(Boolean, default=False)

class TableLab(Base):
    __tablename__ = "tables"
    id = Column(Integer, primary_key=True, index=True)
    table_number = Column(Integer, unique=True)
    vlan_id = Column(Integer)   # 50 + table_number
    student_name = Column(String, nullable=True)

class DeployedResource(Base):
    __tablename__ = "deployed_resources"
    id = Column(Integer, primary_key=True, index=True)
    vmid = Column(Integer, unique=True)
    table_id = Column(Integer, ForeignKey("tables.id"))
    node = Column(String)       # e.g., 'proxmox1'
    type = Column(String)       # 'qemu' or 'lxc'
