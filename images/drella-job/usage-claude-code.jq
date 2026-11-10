# Usage from a Claude Code stream (session.jsonl) [46]: the final `result` event has
# the whole run's usage and cost. Peak context is the largest prompt (input and cache
# tokens) of any assistant turn. Lines that aren't JSON are skipped.
# Use: jq -c -R -n -f usage-claude-code.jq session.jsonl   (prints null when there is nothing)
def n: if type == "number" and . >= 0 then floor else 0 end;
[inputs | try fromjson catch empty | select(type == "object")] as $e
| ([$e[] | select(.type == "result" and (.usage | type == "object"))] | last) as $r
| if $r == null then null else
    ([$e[] | select(.type == "assistant") | .message.usage? | select(type == "object")
      | (.input_tokens | n) + (.cache_read_input_tokens | n) + (.cache_creation_input_tokens | n)]
     | max) as $peak
    | {
        input_tokens: ($r.usage.input_tokens | n),
        output_tokens: ($r.usage.output_tokens | n),
        cache_read_tokens: ($r.usage.cache_read_input_tokens | n),
        cache_write_tokens: ($r.usage.cache_creation_input_tokens | n)
      }
      + (if $peak != null then {peak_context_tokens: $peak} else {} end)
      + (if ($r.total_cost_usd | type) == "number" and $r.total_cost_usd >= 0
         then {cost_usd: $r.total_cost_usd} else {} end)
  end
