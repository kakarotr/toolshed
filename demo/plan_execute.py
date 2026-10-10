import asyncio
import json
import logging

from agent_framework import Agent, Message, tool
from agent_framework_openai import OpenAIChatCompletionClient
from agent_framework_orchestrations import (
    MagenticBuilder,
    MagenticOrchestratorEvent,
    MagenticProgressLedger,
)

logging.basicConfig(level=logging.INFO)


@tool(description="根据集群ID拉取生产环境的底层云资源指标、节点配置、闲置磁盘及异常流量日志。")
def get_cloud_resource_telemetry(resource_id: str):
    print(f"\n[底层系统调用] 正在深度抓取云资源 {resource_id} 的遥测指标与账单...")
    mock_database = {
        "prod-k8s-cluster-01": {
            "service": "Kubernetes Production Cluster",
            "region": "eastus",
            "nodes": [
                {
                    "id": "node-pool-a-1",
                    "instance_type": "Standard_D8s_v5",
                    "avg_cpu_utilization": 12.5,
                    "memory_utilization": 35.0,
                    "monthly_cost_usd": 1200.0,
                },
                {
                    "id": "node-pool-a-2",
                    "instance_type": "Standard_D8s_v5",
                    "avg_cpu_utilization": 8.1,
                    "memory_utilization": 28.0,
                    "monthly_cost_usd": 1200.0,
                },
                {
                    "id": "node-pool-b-gpu-1",
                    "instance_type": "Standard_NC6s_v3",
                    "avg_cpu_utilization": 2.3,
                    "memory_utilization": 10.5,
                    "monthly_cost_usd": 3500.0,
                },
            ],
            "idle_disks": [
                {"disk_id": "disk-data-backup-old", "size_gb": 500, "attached": False, "monthly_cost_usd": 75.0}
            ],
            "traffic_anomaly": "過去7天内，inbound 流量在凌晨 3:00 有异常突增（达到 4.5 Gbps），但随后无数据回写。",
        }
    }
    data = mock_database.get(resource_id, {"error": "未找到该资源ID的遥测记录"})
    return json.dumps(data, ensure_ascii=False, indent=2)


async def main():
    chat_client = OpenAIChatCompletionClient(
        base_url="https://api.deepseek.com",
        model="deepseek-flash",
        api_key="",
    )

    cloud_worker = Agent(
        name="CloudWorker",
        description="专门负责获取并深入解析云资源遥测、成本和指标的执行专家。",
        instructions="你擅长调用工具获取数据，并能从成本、性能、安全维度对云资源底层数据进行深度拆解。",
        client=chat_client,
        tools=[get_cloud_resource_telemetry],
    )

    manager_agent = Agent(
        name="MagenticManager",
        description="资深云架构项目总监与任务规划经理",
        instructions=(
            "你负责统筹管理整个云基础设施诊断项目。\n"
            "当用户提出复杂的运维诊断任务时，你需要：\n"
            "1. 制定清晰的逐步执行计划（Plan）。\n"
            "2. 指挥 cloud_worker 去执行具体的收集和分析动作。\n"
            "3. 监督进展，并在所有数据收集完成后，汇总输出一份高级别的企业级架构优化与降本报告。"
        ),
        client=chat_client,
    )

    workflow = MagenticBuilder(
        participants=[cloud_worker],
        manager_agent=manager_agent,
    ).build()

    task = "请对我们的核心生产集群 'prod-k8s-cluster-01' 进行全方位的健康体检。我们需要识别出所有闲置或低效的资源、高昂的 GPU 浪费点，并给出具体的降本金额与整改路线图。"
    print(f"=== 启动 Magentic 规划执行模式 ===\n任务描述: {task}\n")

    async for event in workflow.run(task, stream=True):
        print(event)
        print("----------")
        if event.type == "magentic_orchestrator":
            if isinstance(event.data.content, Message):
                print(f"Please review the plan:\n{event.data.content.text}")
            elif isinstance(event.data.content, MagenticProgressLedger):
                print(f"Please review progress ledger:\n{json.dumps(event.data.content.to_dict(), indent=2)}")


if __name__ == "__main__":
    asyncio.run(main())
