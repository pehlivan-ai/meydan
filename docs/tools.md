# Tools as the model reads them

An agent works through its tools, and through what they say back. The SWE-agent authors call this
the agent-computer interface (ACI): the actions an agent has, their documentation, and the
feedback the environment gives, all shaped for a language model ([Yang et al., 2024](https://arxiv.org/abs/2405.15793)).
Anthropic's guidance on writing tools says the same of results: return meaningful context, and
errors that say what to do ([Writing effective tools for agents](https://www.anthropic.com/engineering/writing-tools-for-agents)).

meydan's part, in three pieces:

| What the model reads | How to say it | Checked by |
|---|---|---|
| What a tool does and its parameters | the docstring and its `Args:` | `assert_tool_described` |
| What a field of the result means | `Field(description=...)` on the returned type | `assert_tool_described` |
| A rule that said no | `raise ToolError("...")`: the sentence, not a code | |

## Results that say what they mean

The model sees a result's values (`{"places_left": 0}`), not what a field means. Describe the
field and it is sent with the tool:

```python
class Slot(BaseModel):
    time: str = Field(description="When it starts, HH:MM.")
    places_left: int = Field(description="Places still free. 0: full, offer the next time.")


agent = Agent(model, tools=[tool(free_times, context)])
```

`tool(fn, context)` sends the return schema (Pydantic AI's `include_return_schema`: natively for
Gemini, in the tool's description elsewhere) only when a field of the returned type is
described: describing a field is asking for the model to read it. Write what the model should
do with a value, not only what it is.

## Refusals that say what to do

A business rule that fails raises `ToolError`; the model reads `{"error": "..."}` and explains it.
Write the sentence for the model: what happened and what can be done ("This time is full. The
next free time is Sunday 11:00."), not "E_FULL".

## In your tests

```python
from meydan.agent import assert_tool_described
from meydan.reply import assert_described


def test_the_model_reads_nothing_unsaid():
    assert_described(Answer)
    assert_tool_described(*TOOLS)
```
