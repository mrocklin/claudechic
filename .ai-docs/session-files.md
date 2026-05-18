# Claude Session Files

## Location

Sessions stored in `~/.claude/projects/-path-to-project/` where path uses dashes instead of slashes.

```
~/.claude/projects/-Users-username-myproject/
├── 114f29a1-e962-4346-9306-5239fb5d698a.jsonl  # Main sessions (UUID)
├── agent-a4b310b.jsonl                          # Sub-agent sessions
└── *.jsonl.bak                                  # Backups from compaction
```

## JSONL Structure

Each line is a JSON message with `type` field:

- **user**: User messages and tool results
- **assistant**: Claude responses and tool uses
- **system**: System prompts
- **summary**: Compaction summaries (with `isCompactSummary: true`, `leafUuid`)

### Tool Uses (in assistant messages)
```json
{"type": "assistant", "message": {"content": [
  {"type": "tool_use", "id": "toolu_xxx", "name": "Read", "input": {"file_path": "..."}}
]}}
```

### Tool Results (in user messages)
```json
{"type": "user", "message": {"content": [
  {"type": "tool_result", "tool_use_id": "toolu_xxx", "content": "file contents..."}
]}, "toolUseResult": "file contents..."}
```

Note: `toolUseResult` duplicates content (~60% of file size is this duplication).

### Usage Stats (in assistant messages)
```json
{"message": {"usage": {
  "input_tokens": 1000,
  "cache_read_input_tokens": 40000,
  "cache_creation_input_tokens": 500,
  "output_tokens": 200
}}}
```

## Inspection Commands

```bash
# Session file sizes
wc -c ~/.claude/projects/-path-to-project/*.jsonl

# Last usage stats
tail -1 session.jsonl | python3 -c "
import json,sys
d=json.load(sys.stdin)
u=d.get('message',{}).get('usage',{})
print(f'input={u.get(\"input_tokens\",0):,}')
print(f'cache_read={u.get(\"cache_read_input_tokens\",0):,}')
"
```

## Compaction

### Built-in (Claude Code)
- **Microcompact**: Runtime truncation at ~80% context, keeps last ~10 tool results
- **Autocompact**: Creates summary messages, triggered by minTokens/maxTokens thresholds

## Related Code

- `claudechic/sessions.py`: Session listing, loading, context extraction
