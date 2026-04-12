You are compressing raw session conversation memory into a durable summary memory.

You will receive a batch of raw conversation turns from one session. Produce a compact memory summary that preserves the information most useful for future turns.

Requirements:
1. Preserve concrete facts, decisions, constraints, code/file references, errors, user goals, and unresolved follow-ups.
2. Remove filler, repetition, greetings, and conversational noise.
3. Keep the summary durable across future turns. Write it so another model can recover the important context quickly.
4. If the user revealed stable preferences or working style, keep them only if they seem durable.
5. Do not invent facts that do not appear in the source conversation.
6. Keep the summary under roughly {{max_summary_chars}} characters.

Session id: {{session_id}}
Turn count: {{turn_count}}
Source characters: {{source_char_count}}

Raw conversation:
{{source_conversation}}

Return JSON only with this schema:
{
  "summary_title": "short label for this summary batch",
  "summary_text": "compact durable summary text"
}
