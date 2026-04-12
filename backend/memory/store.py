from __future__ import annotations

import json
import uuid
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Sequence

from .config import MemoryConfig
from .errors import MemoryStoreError
from .schemas import ConversationMemoryRecord, PersonaMemoryRecord, SummaryMemoryRecord


class OracleMemoryStore:
    """Oracle-backed persistence layer for conversation, summary, and persona memory."""

    def __init__(self, config: MemoryConfig) -> None:
        self.config = config
        self._pool: Any | None = None
        self._schema_ready = False

    @property
    def is_configured(self) -> bool:
        return self.config.oracle.is_configured

    def describe_status(self) -> dict[str, Any]:
        return {
            "configured": self.is_configured,
            "dsn_present": bool(self.config.oracle.dsn),
            "user_present": bool(self.config.oracle.user),
            "pool_created": self._pool is not None,
            "schema_ready": self._schema_ready,
            "tables": {
                "conversation": self.config.conversation.table_name,
                "summary": self.config.summary.table_name,
                "persona": self.config.persona.table_name,
            },
        }

    def ping(self) -> bool:
        if not self.is_configured:
            return False
        try:
            with self.connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute("select 1 from dual")
                    cursor.fetchone()
            return True
        except Exception:
            return False

    def has_schema(self) -> bool:
        if not self.is_configured:
            return False
        try:
            with self.connection() as connection:
                with connection.cursor() as cursor:
                    required = [
                        self.config.conversation.table_name,
                        self.config.summary.table_name,
                        self.config.persona.table_name,
                    ]
                    cursor.execute(
                        """
                        select table_name
                        from user_tables
                        where table_name in (:conversation_table, :summary_table, :persona_table)
                        """,
                        conversation_table=self.config.conversation.table_name,
                        summary_table=self.config.summary.table_name,
                        persona_table=self.config.persona.table_name,
                    )
                    present = {str(row[0]).upper() for row in cursor.fetchall()}
                    self._schema_ready = all(name in present for name in required)
                    return self._schema_ready
        except Exception:
            return False

    def ensure_schema(self, embedding_dimension: int) -> None:
        if not self.is_configured:
            raise MemoryStoreError("Oracle memory store is not configured. Set ORACLE_USER, ORACLE_PASSWORD, and ORACLE_DSN.")
        if self._schema_ready:
            return

        try:
            with self.connection() as connection:
                with connection.cursor() as cursor:
                    for ddl in self._schema_ddls(embedding_dimension):
                        cursor.execute(ddl)
                connection.commit()
            self._schema_ready = True
        except Exception as exc:
            raise MemoryStoreError(f"Failed to ensure Oracle memory schema: {exc}") from exc

    def insert_conversation_turn(
        self,
        *,
        session_id: str,
        user_message: str,
        assistant_message: str,
        turn_id: str | None = None,
    ) -> ConversationMemoryRecord:
        normalized_turn_id = turn_id or str(uuid.uuid4())
        raw_text = _render_raw_turn(user_message, assistant_message)
        char_count = len(raw_text)

        with self.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    insert into {self.config.conversation.table_name} (
                        turn_id,
                        session_id,
                        user_message,
                        assistant_message,
                        raw_text,
                        char_count,
                        summarized_flag,
                        summary_id,
                        created_at,
                        updated_at
                    ) values (
                        :turn_id,
                        :session_id,
                        :user_message,
                        :assistant_message,
                        :raw_text,
                        :char_count,
                        'N',
                        null,
                        systimestamp,
                        systimestamp
                    )
                    """,
                    turn_id=normalized_turn_id,
                    session_id=session_id,
                    user_message=user_message,
                    assistant_message=assistant_message,
                    raw_text=raw_text,
                    char_count=char_count,
                )
            connection.commit()

        return self.get_conversation_turn(normalized_turn_id)

    def get_conversation_turn(self, turn_id: str) -> ConversationMemoryRecord:
        with self.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    select
                        turn_id,
                        session_id,
                        user_message,
                        assistant_message,
                        raw_text,
                        char_count,
                        summarized_flag,
                        summary_id,
                        created_at,
                        updated_at
                    from {self.config.conversation.table_name}
                    where turn_id = :turn_id
                    """,
                    turn_id=turn_id,
                )
                row = cursor.fetchone()
        if row is None:
            raise MemoryStoreError(f"Conversation turn not found: {turn_id}")
        return _row_to_conversation_record(row)

    def get_unsummarized_turns(self, session_id: str) -> list[ConversationMemoryRecord]:
        with self.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    select
                        turn_id,
                        session_id,
                        user_message,
                        assistant_message,
                        raw_text,
                        char_count,
                        summarized_flag,
                        summary_id,
                        created_at,
                        updated_at
                    from {self.config.conversation.table_name}
                    where session_id = :session_id
                      and summarized_flag = 'N'
                    order by created_at asc
                    """,
                    session_id=session_id,
                )
                rows = cursor.fetchall()
        return [_row_to_conversation_record(row) for row in rows]

    def get_unsummarized_char_count(self, session_id: str) -> int:
        with self.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    select coalesce(sum(char_count), 0)
                    from {self.config.conversation.table_name}
                    where session_id = :session_id
                      and summarized_flag = 'N'
                    """,
                    session_id=session_id,
                )
                row = cursor.fetchone()
        return int(row[0] or 0) if row is not None else 0

    def mark_turns_summarized(self, turn_ids: Sequence[str], *, summary_id: str) -> None:
        if not turn_ids:
            return
        bind_names = {f"turn_id_{index}": value for index, value in enumerate(turn_ids)}
        placeholders = ", ".join(f":{name}" for name in bind_names)
        with self.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    update {self.config.conversation.table_name}
                    set summarized_flag = 'Y',
                        summary_id = :summary_id,
                        updated_at = systimestamp
                    where turn_id in ({placeholders})
                    """,
                    summary_id=summary_id,
                    **bind_names,
                )
            connection.commit()

    def insert_summary(
        self,
        *,
        session_id: str,
        summary_title: str | None,
        summary_text: str,
        source_conversation: str,
        source_turn_ids: Sequence[str],
        source_turn_count: int,
        source_char_count: int,
        embedding: Sequence[float],
        summary_id: str | None = None,
    ) -> SummaryMemoryRecord:
        normalized_summary_id = summary_id or str(uuid.uuid4())
        embedding_value = list(float(item) for item in embedding)

        with self.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    insert into {self.config.summary.table_name} (
                        summary_id,
                        session_id,
                        summary_title,
                        summary_text,
                        source_conversation,
                        source_turn_ids_json,
                        source_turn_count,
                        source_char_count,
                        embedding,
                        created_at,
                        updated_at
                    ) values (
                        :summary_id,
                        :session_id,
                        :summary_title,
                        :summary_text,
                        :source_conversation,
                        :source_turn_ids_json,
                        :source_turn_count,
                        :source_char_count,
                        :embedding,
                        systimestamp,
                        systimestamp
                    )
                    """,
                    summary_id=normalized_summary_id,
                    session_id=session_id,
                    summary_title=summary_title,
                    summary_text=summary_text,
                    source_conversation=source_conversation,
                    source_turn_ids_json=json.dumps(list(source_turn_ids), ensure_ascii=False),
                    source_turn_count=source_turn_count,
                    source_char_count=source_char_count,
                    embedding=embedding_value,
                )
            connection.commit()

        return self.get_summary_by_id(normalized_summary_id)

    def get_summary_by_id(self, summary_id: str) -> SummaryMemoryRecord:
        with self.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    select
                        summary_id,
                        session_id,
                        summary_title,
                        summary_text,
                        source_conversation,
                        source_turn_ids_json,
                        source_turn_count,
                        source_char_count,
                        created_at,
                        updated_at
                    from {self.config.summary.table_name}
                    where summary_id = :summary_id
                    """,
                    summary_id=summary_id,
                )
                row = cursor.fetchone()
        if row is None:
            raise MemoryStoreError(f"Summary memory record not found: {summary_id}")
        return _row_to_summary_record(row)

    def search_summaries(
        self,
        *,
        session_id: str,
        query_vector: Sequence[float],
        top_k: int,
        approximate: bool | None = None,
    ) -> list[SummaryMemoryRecord]:
        normalized_top_k = max(1, int(top_k))
        use_approximate = self.config.summary.use_approximate_search if approximate is None else approximate
        vector_param = list(float(item) for item in query_vector)

        fetch_clause = f"fetch {'approximate ' if use_approximate else ''}first {normalized_top_k} rows only"
        if use_approximate:
            fetch_clause += f" with target accuracy {int(self.config.summary.target_accuracy)}"

        sql = f"""
            select
                summary_id,
                session_id,
                summary_title,
                summary_text,
                source_conversation,
                source_turn_ids_json,
                source_turn_count,
                source_char_count,
                created_at,
                updated_at,
                vector_distance(embedding, :query_vector, {self.config.summary.distance}) as score
            from {self.config.summary.table_name}
            where session_id = :session_id
            order by vector_distance(embedding, :query_vector, {self.config.summary.distance})
            {fetch_clause}
        """

        try:
            with self.connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(sql, session_id=session_id, query_vector=vector_param)
                    rows = cursor.fetchall()
        except Exception:
            if use_approximate:
                return self.search_summaries(
                    session_id=session_id,
                    query_vector=query_vector,
                    top_k=top_k,
                    approximate=False,
                )
            raise

        return [_row_to_summary_record(row) for row in rows]

    def list_persona_entries(
        self,
        *,
        owner_scope: str,
        limit: int,
        always_include_only: bool = False,
    ) -> list[PersonaMemoryRecord]:
        normalized_limit = max(1, int(limit))
        where_clauses = ["owner_scope = :owner_scope"]
        if always_include_only:
            where_clauses.append("always_include = 'Y'")

        sql = f"""
            select
                persona_id,
                owner_scope,
                memory_key,
                memory_value,
                category,
                importance,
                always_include,
                source_note,
                created_at,
                updated_at
            from {self.config.persona.table_name}
            where {' and '.join(where_clauses)}
            order by always_include desc, importance desc, updated_at desc
            fetch first {normalized_limit} rows only
        """

        with self.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(sql, owner_scope=owner_scope)
                rows = cursor.fetchall()
        return [_row_to_persona_record(row) for row in rows]

    def upsert_persona_entry(
        self,
        *,
        owner_scope: str,
        memory_key: str,
        memory_value: str,
        category: str,
        importance: float,
        always_include: bool,
        source_note: str | None,
    ) -> PersonaMemoryRecord:
        normalized_persona_id = str(uuid.uuid4())
        with self.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    merge into {self.config.persona.table_name} target
                    using (
                        select
                            :owner_scope as owner_scope,
                            :memory_key as memory_key
                        from dual
                    ) src
                    on (
                        target.owner_scope = src.owner_scope
                        and target.memory_key = src.memory_key
                    )
                    when matched then update set
                        memory_value = :memory_value,
                        category = :category,
                        importance = :importance,
                        always_include = :always_include,
                        source_note = :source_note,
                        updated_at = systimestamp
                    when not matched then insert (
                        persona_id,
                        owner_scope,
                        memory_key,
                        memory_value,
                        category,
                        importance,
                        always_include,
                        source_note,
                        created_at,
                        updated_at
                    ) values (
                        :persona_id,
                        :owner_scope,
                        :memory_key,
                        :memory_value,
                        :category,
                        :importance,
                        :always_include,
                        :source_note,
                        systimestamp,
                        systimestamp
                    )
                    """,
                    persona_id=normalized_persona_id,
                    owner_scope=owner_scope,
                    memory_key=memory_key,
                    memory_value=memory_value,
                    category=category,
                    importance=float(importance),
                    always_include="Y" if always_include else "N",
                    source_note=source_note,
                )
            connection.commit()

        with self.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    select
                        persona_id,
                        owner_scope,
                        memory_key,
                        memory_value,
                        category,
                        importance,
                        always_include,
                        source_note,
                        created_at,
                        updated_at
                    from {self.config.persona.table_name}
                    where owner_scope = :owner_scope
                      and memory_key = :memory_key
                    """,
                    owner_scope=owner_scope,
                    memory_key=memory_key,
                )
                row = cursor.fetchone()
        if row is None:
            raise MemoryStoreError(f"Persona memory entry not found after upsert: {memory_key}")
        return _row_to_persona_record(row)

    def close(self) -> None:
        if self._pool is None:
            return
        try:
            self._pool.close(force=True)
        finally:
            self._pool = None
            self._schema_ready = False

    @contextmanager
    def connection(self):
        pool = self._ensure_pool()
        connection = pool.acquire()
        try:
            yield connection
        finally:
            pool.release(connection)

    def _ensure_pool(self):
        if self._pool is not None:
            return self._pool
        if not self.is_configured:
            raise MemoryStoreError("Oracle memory store is not configured. Set ORACLE_USER, ORACLE_PASSWORD, and ORACLE_DSN.")

        try:
            import oracledb
        except Exception as exc:
            raise MemoryStoreError("python-oracledb is not installed in the active environment.") from exc

        try:
            self._pool = oracledb.create_pool(
                user=self.config.oracle.user,
                password=self.config.oracle.password,
                dsn=self.config.oracle.dsn,
                min=self.config.oracle.min_pool_size,
                max=self.config.oracle.max_pool_size,
                increment=self.config.oracle.pool_increment,
                ping_interval=self.config.oracle.ping_interval_seconds,
                stmtcachesize=self.config.oracle.stmt_cache_size,
            )
            return self._pool
        except Exception as exc:
            raise MemoryStoreError(f"Failed to create Oracle connection pool: {exc}") from exc

    def _schema_ddls(self, embedding_dimension: int) -> list[str]:
        conversation_table = self.config.conversation.table_name
        summary_table = self.config.summary.table_name
        persona_table = self.config.persona.table_name
        session_index = self.config.summary.session_index_name
        vector_index = self.config.summary.vector_index_name
        neighbor_partitions = self.config.summary.ivf_neighbor_partitions
        distance = self.config.summary.distance

        return [
            f"""
            begin
                execute immediate '
                    create table {conversation_table} (
                        turn_id varchar2(64) primary key,
                        session_id varchar2(128) not null,
                        user_message clob not null,
                        assistant_message clob not null,
                        raw_text clob not null,
                        char_count number not null,
                        summarized_flag char(1) default ''N'' not null,
                        summary_id varchar2(64),
                        created_at timestamp with time zone default systimestamp not null,
                        updated_at timestamp with time zone default systimestamp not null,
                        constraint {conversation_table}_sf_chk check (summarized_flag in (''Y'', ''N''))
                    )
                ';
            exception
                when others then
                    if sqlcode != -955 then
                        raise;
                    end if;
            end;
            """,
            f"""
            begin
                execute immediate '
                    create table {summary_table} (
                        summary_id varchar2(64) primary key,
                        session_id varchar2(128) not null,
                        summary_title varchar2(512),
                        summary_text clob not null,
                        source_conversation clob not null,
                        source_turn_ids_json clob,
                        source_turn_count number not null,
                        source_char_count number not null,
                        embedding vector({embedding_dimension}, FLOAT32) not null,
                        created_at timestamp with time zone default systimestamp not null,
                        updated_at timestamp with time zone default systimestamp not null
                    )
                ';
            exception
                when others then
                    if sqlcode != -955 then
                        raise;
                    end if;
            end;
            """,
            f"""
            begin
                execute immediate '
                    create index {session_index}
                    on {summary_table} (session_id, created_at)
                ';
            exception
                when others then
                    if sqlcode != -955 then
                        raise;
                    end if;
            end;
            """,
            f"""
            begin
                execute immediate '
                    create vector index {vector_index}
                    on {summary_table} (embedding)
                    organization neighbor partitions
                    distance {distance}
                    with target accuracy {int(self.config.summary.target_accuracy)}
                    parameters (type IVF, neighbor partitions {neighbor_partitions})
                ';
            exception
                when others then
                    if sqlcode != -955 then
                        raise;
                    end if;
            end;
            """,
            f"""
            begin
                execute immediate '
                    create table {persona_table} (
                        persona_id varchar2(64) primary key,
                        owner_scope varchar2(128) not null,
                        memory_key varchar2(256) not null,
                        memory_value clob not null,
                        category varchar2(128) not null,
                        importance binary_double default 0.5 not null,
                        always_include char(1) default ''N'' not null,
                        source_note clob,
                        created_at timestamp with time zone default systimestamp not null,
                        updated_at timestamp with time zone default systimestamp not null,
                        constraint {persona_table}_ai_chk check (always_include in (''Y'', ''N'')),
                        constraint {persona_table}_uq unique (owner_scope, memory_key)
                    )
                ';
            exception
                when others then
                    if sqlcode != -955 then
                        raise;
                    end if;
            end;
            """,
        ]


def _render_raw_turn(user_message: str, assistant_message: str) -> str:
    return f"User:\\n{user_message.strip()}\\n\\nAssistant:\\n{assistant_message.strip()}".strip()


def _row_to_conversation_record(row: Sequence[Any]) -> ConversationMemoryRecord:
    return ConversationMemoryRecord(
        turn_id=str(row[0]),
        session_id=str(row[1]),
        user_message=_clob_to_text(row[2]),
        assistant_message=_clob_to_text(row[3]),
        raw_text=_clob_to_text(row[4]),
        char_count=int(row[5]),
        summarized=str(row[6]) == "Y",
        summary_id=str(row[7]) if row[7] is not None else None,
        created_at=_datetime_to_iso(row[8]),
        updated_at=_datetime_to_iso(row[9]),
    )


def _row_to_summary_record(row: Sequence[Any]) -> SummaryMemoryRecord:
    source_turn_ids = []
    if row[5]:
        try:
            source_turn_ids = [str(item) for item in json.loads(_clob_to_text(row[5]))]
        except json.JSONDecodeError:
            source_turn_ids = []
    score = float(row[10]) if len(row) >= 11 and row[10] is not None else None
    return SummaryMemoryRecord(
        summary_id=str(row[0]),
        session_id=str(row[1]),
        summary_title=str(row[2]) if row[2] is not None else None,
        summary_text=_clob_to_text(row[3]),
        source_conversation=_clob_to_text(row[4]),
        source_turn_ids=source_turn_ids,
        source_turn_count=int(row[6]),
        source_char_count=int(row[7]),
        score=score,
        created_at=_datetime_to_iso(row[8]),
        updated_at=_datetime_to_iso(row[9]),
    )


def _row_to_persona_record(row: Sequence[Any]) -> PersonaMemoryRecord:
    return PersonaMemoryRecord(
        persona_id=str(row[0]),
        owner_scope=str(row[1]),
        memory_key=str(row[2]),
        memory_value=_clob_to_text(row[3]),
        category=str(row[4]),
        importance=float(row[5]),
        always_include=str(row[6]) == "Y",
        source_note=_clob_to_text(row[7]) if row[7] is not None else None,
        created_at=_datetime_to_iso(row[8]),
        updated_at=_datetime_to_iso(row[9]),
    )


def _clob_to_text(value: Any) -> str:
    if value is None:
        return ""
    read = getattr(value, "read", None)
    if callable(read):
        return str(read())
    return str(value)


def _datetime_to_iso(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    if value is None:
        return ""
    return str(value)
