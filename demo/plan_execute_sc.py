import asyncio

from agentscope.agent import Agent
from agentscope.credential import OpenAICredential
from agentscope.event import AgentEvent
from agentscope.message import UserMsg
from agentscope.model import ChatModelBase, OpenAIChatModel
from agentscope.tool import TaskCreate, TaskGet, TaskList, TaskUpdate, Toolkit
from pydantic import SecretStr

toolkit = Toolkit(
    tools=[
        TaskCreate(),
        TaskGet(),
        TaskList(),
        TaskUpdate(),
    ]
)

agent = Agent(
    name="planner",
    system_prompt="You are a planning assistant.",
    model=OpenAIChatModel(
        model="deepseek-flash",
        credential=OpenAICredential(
            base_url="https://api.deepseek.com/v1", api_key=SecretStr("sk-")
        ),
        extra_body={"thinking": {"type": "disabled"}},
    ),
)


async def main():
    msg = agent.reply_stream(
        UserMsg(name="user", content="我要在周末办一场 10 人的家庭烧烤聚会，请帮我规划并逐步推进各项准备工作。")
    )

    async for event in msg:
        if isinstance(event, AgentEvent):
            if event.type != "TEXT_BLOCK_DELTA":
                print(event)


if __name__ == "__main__":
    asyncio.run(main())
