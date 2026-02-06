# K8s Driver 实现分析

## 1. 架构兼容性评估

**结论：架构设计良好，添加 K8s 驱动比较方便。**

Bay 采用了清晰的分层架构，Driver 层完全抽象，上层业务代码不依赖具体实现：

```
┌─────────────────────────────────────────┐
│  Managers (SandboxManager, SessionManager, CargoManager)
│    ↓ 只依赖 Driver 抽象接口
├─────────────────────────────────────────┤
│  Driver Interface (app/drivers/base.py)
│    - 12 个抽象方法，接口清晰
│    - 容器生命周期：create/start/stop/destroy/status/logs
│    - Volume 管理：create_volume/delete_volume/volume_exists
│    - GC 发现：list_runtime_instances/destroy_runtime_instance
├─────────────────────────────────────────┤
│  DockerDriver (app/drivers/docker/docker.py)
│    - aiodocker 实现
│    - ~500 行代码
└─────────────────────────────────────────┘
```

## 2. 需要实现的 Driver 接口方法

```python
class K8sDriver(Driver):
    # 容器/Pod 生命周期
    async def create(session, profile, cargo, labels) -> str  # 创建 Pod
    async def start(container_id, runtime_port) -> str        # 启动 Pod，返回 endpoint
    async def stop(container_id) -> None                      # 删除 Pod (K8s 无 stop 概念)
    async def destroy(container_id) -> None                   # 删除 Pod
    async def status(container_id, runtime_port) -> ContainerInfo  # 获取 Pod 状态
    async def logs(container_id, tail) -> str                 # 获取 Pod 日志

    # Volume/PVC 管理
    async def create_volume(name, labels) -> str              # 创建 PVC
    async def delete_volume(name) -> None                     # 删除 PVC
    async def volume_exists(name) -> bool                     # 检查 PVC 是否存在

    # GC 发现
    async def list_runtime_instances(labels) -> list[RuntimeInstance]  # 列出 Pod
    async def destroy_runtime_instance(instance_id) -> None   # 强制删除 Pod
```

## 3. K8s 特殊考虑

### 3.1 Pod vs Container

| Docker | K8s |
|--------|-----|
| Container | Pod |
| Volume | PVC (PersistentVolumeClaim) |
| docker.sock | kubeconfig / in-cluster config |
| Container IP | Pod IP / Service ClusterIP |
| Port mapping | Service NodePort / LoadBalancer |

### 3.2 Endpoint 解析策略

```python
# Pod IP 直连 (唯一方案)
# Bay 作为唯一网关部署在 K8s 集群内，直接访问 Pod IP
# 外部流量：Client -> Bay (Ingress/LB 暴露) -> Pod IP
endpoint = f"http://{pod_ip}:{runtime_port}"
```

**架构设计：**
- Bay 是唯一暴露给外部的服务（通过 Ingress/LoadBalancer）
- Ship Pod 只在集群内部通信，不需要 Service/Ingress
- 与 Docker 模式的 `container_network` 模式完全对应

```
┌──────────────────────────────────────────────────┐
│  K8s Cluster                                     │
│                                                  │
│  ┌─────────────────────────────────────────────┐ │
│  │  Bay Pod (Deployment + Ingress/LB 暴露)      │ │
│  │    ↓ Pod IP 直连                            │ │
│  ├─────────────────────────────────────────────┤ │
│  │  Ship Pod 1  │  Ship Pod 2  │  Ship Pod 3   │ │
│  │  (无需 Svc)  │  (无需 Svc)  │  (无需 Svc)   │ │
│  └─────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────┘
```

### 3.3 配置项 (已预留)

[`config.py`](pkgs/bay/app/config.py:60-65) 中已有 `K8sConfig`：

```python
class K8sConfig(BaseModel):
    namespace: str = "bay"
    kubeconfig: str | None = None  # None = in-cluster config
    # 需要扩展：
    # storage_class: str = "standard"
    # image_pull_secrets: list[str] = []
    # service_type: Literal["ClusterIP", "NodePort"] = "ClusterIP"
```

## 4. 工作量估算

### 4.1 核心实现 (M 档)

| 任务 | 复杂度 | 说明 |
|------|--------|------|
| K8sDriver 基础实现 | 中 | ~600 行代码，参考 DockerDriver |
| Pod 创建/启动/停止 | 中 | kubernetes-asyncio 库 |
| PVC 管理 | 低 | 简单 CRUD |
| 状态映射 | 低 | Pod Phase -> ContainerStatus |
| Endpoint 解析 | 中 | 支持 Pod IP / Service |

### 4.2 配置扩展 (S 档)

| 任务 | 复杂度 | 说明 |
|------|--------|------|
| K8sConfig 扩展 | 低 | 添加 storage_class 等字段 |
| dependencies.py 修改 | 低 | get_driver() 添加 k8s 分支 |
| 单元测试 | 中 | Mock K8s API |

### 4.3 可选增强 (L 档)

| 任务 | 复杂度 | 说明 |
|------|--------|------|
| Resource Quota | 中 | namespace 级别资源限制 |
| SecurityContext | 中 | Pod 安全策略 |
| Tolerations/Affinity | 中 | 调度控制 |
| NetworkPolicy | 中 | Pod 间网络隔离 |
| Pod Disruption Budget | 低 | 可用性保障 |

> **注意**：Service/Ingress 不在计划内 —— Bay 作为唯一网关暴露，Ship Pod 只需 Pod IP 直连。

## 5. 需要修改的文件

```
pkgs/bay/
├── app/
│   ├── config.py                     # 扩展 K8sConfig (+30 行)
│   ├── api/dependencies.py           # get_driver() 添加 k8s 分支 (+10 行)
│   ├── drivers/
│   │   ├── __init__.py               # 导出 K8sDriver
│   │   └── k8s/                       # 新目录
│   │       ├── __init__.py           # +5 行
│   │       └── k8s.py                # 主实现 (~600 行)
│   └── models/cargo.py               # backend 字段已支持 k8s_pvc ✓
├── pyproject.toml                    # 添加 kubernetes-asyncio 依赖
└── tests/
    └── unit/drivers/
        └── test_k8s_driver.py        # 单元测试 (~300 行)
```

## 6. 依赖库

```toml
# pyproject.toml
dependencies = [
    # 现有依赖...
    "kubernetes-asyncio>=29.0.0",  # 异步 K8s 客户端
]
```

## 7. 实现示例 (核心方法)

```python
# pkgs/bay/app/drivers/k8s/k8s.py

from kubernetes_asyncio import client, config
from kubernetes_asyncio.client.api_client import ApiClient

class K8sDriver(Driver):
    def __init__(self) -> None:
        settings = get_settings()
        k8s_cfg = settings.driver.k8s
        self._namespace = k8s_cfg.namespace
        self._kubeconfig = k8s_cfg.kubeconfig
        self._client: ApiClient | None = None

    async def _get_client(self) -> ApiClient:
        if self._client is None:
            if self._kubeconfig:
                await config.load_kube_config(config_file=self._kubeconfig)
            else:
                config.load_incluster_config()
            self._client = ApiClient()
        return self._client

    async def create(self, session, profile, cargo, labels=None) -> str:
        api = await self._get_client()
        v1 = client.CoreV1Api(api)

        pod_name = f"bay-session-{session.id}"
        pod = client.V1Pod(
            metadata=client.V1ObjectMeta(
                name=pod_name,
                namespace=self._namespace,
                labels={
                    "bay.session_id": session.id,
                    "bay.sandbox_id": session.sandbox_id,
                    "bay.cargo_id": cargo.id,
                    "bay.managed": "true",
                    **(labels or {}),
                },
            ),
            spec=client.V1PodSpec(
                containers=[
                    client.V1Container(
                        name="ship",
                        image=profile.image,
                        ports=[client.V1ContainerPort(container_port=profile.runtime_port)],
                        volume_mounts=[
                            client.V1VolumeMount(
                                name="workspace",
                                mount_path="/workspace",
                            )
                        ],
                        resources=client.V1ResourceRequirements(
                            limits={
                                "cpu": str(profile.resources.cpus),
                                "memory": profile.resources.memory,
                            },
                        ),
                    )
                ],
                volumes=[
                    client.V1Volume(
                        name="workspace",
                        persistent_volume_claim=client.V1PersistentVolumeClaimVolumeSource(
                            claim_name=cargo.driver_ref,
                        ),
                    )
                ],
            ),
        )

        await v1.create_namespaced_pod(namespace=self._namespace, body=pod)
        return pod_name

    async def start(self, container_id: str, runtime_port: int) -> str:
        # K8s Pod 创建即启动，需要等待 Running
        api = await self._get_client()
        v1 = client.CoreV1Api(api)

        # 轮询等待 Pod Ready
        for _ in range(60):  # 最多等待 60 秒
            pod = await v1.read_namespaced_pod(name=container_id, namespace=self._namespace)
            if pod.status.phase == "Running" and pod.status.pod_ip:
                return f"http://{pod.status.pod_ip}:{runtime_port}"
            await asyncio.sleep(1)

        raise SessionNotReadyError("Pod failed to start")

    async def create_volume(self, name: str, labels=None) -> str:
        api = await self._get_client()
        v1 = client.CoreV1Api(api)

        pvc = client.V1PersistentVolumeClaim(
            metadata=client.V1ObjectMeta(
                name=name,
                namespace=self._namespace,
                labels={"bay.managed": "true", **(labels or {})},
            ),
            spec=client.V1PersistentVolumeClaimSpec(
                access_modes=["ReadWriteOnce"],
                resources=client.V1VolumeResourceRequirements(
                    requests={"storage": "1Gi"},
                ),
                # storage_class_name=self._storage_class,
            ),
        )

        await v1.create_namespaced_persistent_volume_claim(
            namespace=self._namespace, body=pvc
        )
        return name
```

## 8. 总结

| 维度 | 评估 |
|------|------|
| **架构兼容性** | ⭐⭐⭐⭐⭐ 非常好，Driver 抽象干净 |
| **实现难度** | ⭐⭐⭐☆☆ 中等，K8s API 比 Docker 复杂 |
| **代码量** | ~600-800 行新代码 |
| **测试覆盖** | 需要 Mock K8s API 的单元测试 |
| **文档** | 需要 K8s 部署指南 |

**建议分阶段实施：**

1. **Phase 1**: 基础实现 (Pod + PVC + Pod IP 直连)
2. **Phase 2**: 生产加固 (SecurityContext + Resource Quota + NetworkPolicy)
3. **Phase 3**: 高可用 (PodDisruptionBudget + Tolerations + Affinity)

> Bay 作为唯一网关暴露，Ship Pod 只需 Pod IP 直连，不需要为每个 Pod 创建 Service/Ingress。
