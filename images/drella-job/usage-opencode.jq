# Usage from an OpenCode export (session.json) [46]: sums the token counts of the
# assistant messages. Reasoning tokens are billed as output. OpenCode reports a
# cost per message, but not one the model's prices are guaranteed to match, so none
# is sent: Drella prices the tokens from the job's model snapshot. Peak context is the
# largest prompt (input and cache tokens) any one message sent.
# Use: jq -c -f usage-opencode.jq session.json   (prints null when there is nothing)
def n: if type == "number" and . >= 0 then floor else 0 end;
[.messages[]?.info | select(.role == "assistant" and (.tokens | type == "object")) | .tokens] as $t
| if ($t | length) == 0 then null else {
    input_tokens: ([$t[].input | n] | add),
    output_tokens: ([$t[] | (.output | n) + (.reasoning | n)] | add),
    cache_read_tokens: ([$t[].cache.read? | n] | add),
    cache_write_tokens: ([$t[].cache.write? | n] | add),
    peak_context_tokens: ([$t[] | (.input | n) + (.cache.read? | n) + (.cache.write? | n)] | max)
  } end
