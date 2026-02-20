# Kubernetes 生产部署

Bay + Ship + Gull 的 Kubernetes 生产部署方案。

## 架构

```
┌───────────────────────────────────────────────────────────┐
│  K8s Cluster                                              │
│                                                           │
│  Namespace: bay                                           │
│  ┌──────────────────────────────────────────────────────┐ │
│  │                                                      │ │
│  │  ┌──────────────┐                                    │ │
│  │  │  Bay         │  Service (LoadBalancer :8114)       │ │
│  │  │  Deployment  │  ←── 外部访问入口                   │ │
│  │  │  (1 replica) │                                    │ │
│  │  └──────┬───────┘                                    │ │
│  │         │  in-cluster config → K8s API                │ │
│  │         │                                             │ │
│  │         ├──→ Ship Pod (sandbox-xxx)  ← 动态创建       │ │
│  │         ├──→ Ship Pod (sandbox-yyy)  ← 动态创建       │ │
│  │         └──→ Gull Pod (sandbox-zzz)  ← 动态创建       │ │
│  │                                                      │ │
│  │  ┌──────────┐  ┌──────────┐                          │ │
│  │  │ bay-data │  │ PVC      │  ← Cargo 动态 PVC        │ │
│  │  │ PVC      │  │ (per     │                          │ │
│  │  │ (SQLite) │  │  sandbox)│                          │ │
│  │  └──────────┘  └──────────┘                          │ │
│  └──────────────────────────────────────────────────────┘ │
└───────────────────────────────────────────────────────────┘
```

**与 Docker 部署的区别**：
- K8s 模式下，Bay 通过 K8s API 动态创建 sandbox Pod 和 PVC（而非 Docker Socket）
- 每个 sandbox 的 Cargo 存储使用独立的 PVC（而非宿主机目录）
- Bay 通过 Pod IP 直连 sandbox Pod（无需 Docker 网络配置）

## 前置条件

- Kubernetes 集群 1.24+
- kubectl 已配置并连接到集群
- 集群有可用的默认 StorageClass（或在 PVC 中指定）
- 对于 LoadBalancer Service：集群支持 LoadBalancer（云环境或 MetalLB）

## 快速开始

```bash
# 1. 编辑 ConfigMap，搜索 CHANGE-ME 修改必须项：
#    - security.api_key → 设置强随机密钥
#      （若同时设置 BAY_API_KEY 环境变量，则 BAY_API_KEY 优先）
vi 02-configmap.yaml

# 2. 按序部署
kubectl apply -f 00-namespace.yaml
kubectl apply -f 01-rbac.yaml
kubectl apply -f 02-configmap.yaml
kubectl apply -f 03-pvc.yaml
kubectl apply -f 04-deployment.yaml
kubectl apply -f 05-service.yaml

# 或一键部署
kubectl apply -f .

# 3. 检查部署状态
kubectl -n bay get pods
kubectl -n bay get svc

# 4. 验证健康
# LoadBalancer 分配 IP 后：
curl http://<EXTERNAL-IP>:8114/health

# 或通过 port-forward：
kubectl -n bay port-forward svc/bay 8114:8114
curl http://localhost:8114/health
```

## 文件说明

| 文件 | 说明 |
|------|------|
| `00-namespace.yaml` | Namespace `bay` |
| `01-rbac.yaml` | ServiceAccount + Role + RoleBinding（Bay 创建 Pod/PVC 的权限） |
| `02-configmap.yaml` | Bay 配置文件（profiles、driver、GC 等） |
| `03-pvc.yaml` | Bay 数据持久化（SQLite），默认 StorageClass，可选指定 |
| `04-deployment.yaml` | Bay Deployment（1 副本，含健康检查） |
| `05-service.yaml` | LoadBalancer Service（暴露 :8114） |

## RBAC 权限

Bay 需要以下最小权限来管理 sandbox：

| 资源 | 操作 | 用途 |
|------|------|------|
| `pods` | create, delete, get, list, watch | 创建和管理 sandbox Pod |
| `pods/log` | get | 读取 sandbox 日志 |
| `persistentvolumeclaims` | create, delete, get, list, watch | 创建和管理 Cargo PVC |

## 自定义配置

### 指定 StorageClass

如果集群没有默认 StorageClass 或需要指定，在以下文件中取消注释：

```yaml
# 03-pvc.yaml - Bay 数据 PVC
spec:
  storageClassName: "your-storage-class"

# 02-configmap.yaml - sandbox 动态 PVC
driver:
  k8s:
    storage_class: "your-storage-class"
```

### 使用私有 Registry

如果使用私有 registry（如 Harbor），需要创建 image pull secret：

```bash
# 创建 secret
kubectl -n bay create secret docker-registry ghcr-secret \
  --docker-server=ghcr.io \
  --docker-username=<username> \
  --docker-password=<token>

# 在 02-configmap.yaml 的 k8s 配置中添加
image_pull_secrets: ["ghcr-secret"]

# 在 04-deployment.yaml 的 spec 中添加
spec:
  imagePullSecrets:
    - name: ghcr-secret
```

### 改为 NodePort

如果集群不支持 LoadBalancer，编辑 `05-service.yaml`：

```yaml
spec:
  type: NodePort
  ports:
    - name: http
      port: 8114
      targetPort: 8114
      nodePort: 30800  # 固定端口
```

## 运维

### 查看状态

```bash
# Bay Pod
kubectl -n bay get pods -l app.kubernetes.io/name=bay
kubectl -n bay logs -f deployment/bay

# sandbox Pod（Bay 动态创建的）
kubectl -n bay get pods -l bay.managed=true

# sandbox PVC
kubectl -n bay get pvc -l bay.managed=true
```

### 手动触发 GC

```bash
curl -X POST -H "Authorization: Bearer <api-key>" \
  http://<bay-endpoint>:8114/v1/admin/gc/run
```

### 升级

```bash
# 更新镜像版本
kubectl -n bay set image deployment/bay bay=ghcr.io/astrbotdevs/shipyard-neo-bay:v0.2.0

# 或更新 ConfigMap 后重启
kubectl apply -f 02-configmap.yaml
kubectl -n bay rollout restart deployment/bay
```

### 备份

```bash
# 备份 SQLite 数据库
kubectl -n bay cp deployment/bay:/app/data/bay.db ./bay-backup.db
```

## 安全建议

1. **必须配置 API Key** — 建议至少设置 `security.api_key`；若同时设置 `BAY_API_KEY`，以环境变量为准
2. **使用 Ingress + TLS** — 在 Bay 前部署 Ingress Controller 进行 TLS 终止
3. **RBAC 最小权限** — 已配置为仅允许 Bay namespace 内操作
4. **Pod Security** — 建议配置 PodSecurityAdmission 限制 sandbox Pod 权限
5. **Network Policy** — 建议配置 NetworkPolicy 限制 sandbox Pod 的出站流量
