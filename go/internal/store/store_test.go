package store

// Opt-in Postgres tests, mirroring the Python suite's TEST_DATABASE_URL
// discipline (tests/conftest.py, tests/test_postgres_bootstrap.py): unset
// env var => skip, so `go test ./...` stays green on a machine with no
// Postgres, exactly like pytest. The target database is DISPOSABLE -- every
// test drops and recreates schema public from the committed snapshot. NEVER
// point TEST_DATABASE_URL at a real database.
//
// What these prove is the PR-A contract: the committed schema snapshot and
// the sqlc-generated queries agree with a real Postgres -- constraints,
// ownership scoping, soft-delete and upsert-replace semantics included. Wire
// behavior (status codes, cookie flags, response shapes) is PR B/C's
// contract-test territory, not this file's.

import (
	"context"
	_ "embed"
	"errors"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgtype"
	"github.com/jackc/pgx/v5/pgxpool"
)

//go:embed schema.sql
var schemaSQL string

func ts(t time.Time) pgtype.Timestamp {
	return pgtype.Timestamp{Time: t, Valid: true}
}

func text(s string) pgtype.Text {
	return pgtype.Text{String: s, Valid: true}
}

// splitStatements breaks the pg_dump snapshot into executable statements.
// The snapshot holds only table/sequence/index/RLS DDL -- no function bodies,
// so "line ends with ;" is a correct statement boundary here by construction
// (scripts/gen-store-schema.sh keeps it that way).
func splitStatements(sql string) []string {
	var stmts []string
	var b strings.Builder
	for _, line := range strings.Split(sql, "\n") {
		trimmed := strings.TrimSpace(line)
		if trimmed == "" || strings.HasPrefix(trimmed, "--") {
			continue
		}
		b.WriteString(line)
		b.WriteString("\n")
		if strings.HasSuffix(trimmed, ";") {
			stmts = append(stmts, b.String())
			b.Reset()
		}
	}
	return stmts
}

func testQueries(t *testing.T) (*pgxpool.Pool, *Queries) {
	t.Helper()
	dbURL := os.Getenv("TEST_DATABASE_URL")
	if dbURL == "" {
		t.Skip("TEST_DATABASE_URL not set (postgres store tests are opt-in)")
	}
	ctx := t.Context()
	pool, err := NewPool(ctx, dbURL)
	if err != nil {
		t.Fatalf("NewPool: %v", err)
	}
	t.Cleanup(pool.Close)
	stmts := append([]string{"DROP SCHEMA public CASCADE", "CREATE SCHEMA public"},
		splitStatements(schemaSQL)...)
	for _, stmt := range stmts {
		if _, err := pool.Exec(ctx, stmt); err != nil {
			t.Fatalf("bootstrap statement failed: %v\n%s", err, stmt)
		}
	}
	return pool, New(pool)
}

func seedUser(t *testing.T, ctx context.Context, pool *pgxpool.Pool, email string) int32 {
	t.Helper()
	var id int32
	err := pool.QueryRow(ctx,
		`INSERT INTO public."user" (email, password_hash, display_name, role, is_active, created_at)
		 VALUES ($1, 'x', 'Test', 'analyst', true, now()) RETURNING id`, email).Scan(&id)
	if err != nil {
		t.Fatalf("seed user: %v", err)
	}
	return id
}

func seedQueryLog(t *testing.T, ctx context.Context, pool *pgxpool.Pool, userID, mode string) int32 {
	t.Helper()
	var id int32
	err := pool.QueryRow(ctx,
		`INSERT INTO public.query_log (ts, user_id, mode, query_text, answer_text, refused, model_name)
		 VALUES (now(), $1, $2, 'q', 'a', false, 'm') RETURNING id`, userID, mode).Scan(&id)
	if err != nil {
		t.Fatalf("seed query_log: %v", err)
	}
	return id
}

func TestAuthSessionLifecycle(t *testing.T) {
	pool, q := testQueries(t)
	ctx := t.Context()
	uid := seedUser(t, ctx, pool, "a@example.com")
	now := time.Now().UTC().Truncate(time.Microsecond)

	sid, err := q.CreateAuthSession(ctx, CreateAuthSessionParams{
		TokenHash: "h1", UserID: uid, CreatedAt: ts(now), ExpiresAt: ts(now.Add(72 * time.Hour)),
	})
	if err != nil || sid == 0 {
		t.Fatalf("CreateAuthSession: id=%d err=%v", sid, err)
	}

	row, err := q.GetAuthSessionWithUser(ctx, "h1")
	if err != nil {
		t.Fatalf("GetAuthSessionWithUser: %v", err)
	}
	if row.Email != "a@example.com" || !row.IsActive || row.UserID != uid {
		t.Fatalf("unexpected session row: %+v", row)
	}
	if !row.ExpiresAt.Time.Equal(now.Add(72 * time.Hour)) {
		t.Fatalf("expires_at drifted: %v", row.ExpiresAt.Time)
	}
	if row.LastSeenAt.Valid {
		t.Fatalf("last_seen_at should start NULL")
	}

	if err := q.TouchAuthSessionLastSeen(ctx, TouchAuthSessionLastSeenParams{
		TokenHash: "h1", LastSeenAt: ts(now),
	}); err != nil {
		t.Fatalf("TouchAuthSessionLastSeen: %v", err)
	}
	row, err = q.GetAuthSessionWithUser(ctx, "h1")
	if err != nil || !row.LastSeenAt.Valid {
		t.Fatalf("last_seen_at not persisted: %+v err=%v", row, err)
	}

	// Revoke is idempotent-by-rows: first delete reports 1, a repeat reports
	// 0 -- the Python logout's "silent if absent" contract.
	if n, err := q.DeleteAuthSessionByTokenHash(ctx, "h1"); err != nil || n != 1 {
		t.Fatalf("revoke: n=%d err=%v", n, err)
	}
	if n, err := q.DeleteAuthSessionByTokenHash(ctx, "h1"); err != nil || n != 0 {
		t.Fatalf("repeat revoke: n=%d err=%v", n, err)
	}
	if _, err := q.GetAuthSessionWithUser(ctx, "h1"); !errors.Is(err, pgx.ErrNoRows) {
		t.Fatalf("expected ErrNoRows after revoke, got %v", err)
	}
}

func TestDeleteExpiredAuthSessions(t *testing.T) {
	pool, q := testQueries(t)
	ctx := t.Context()
	uid := seedUser(t, ctx, pool, "b@example.com")
	now := time.Now().UTC().Truncate(time.Microsecond)

	mk := func(hash string, expires time.Time) {
		t.Helper()
		if _, err := q.CreateAuthSession(ctx, CreateAuthSessionParams{
			TokenHash: hash, UserID: uid, CreatedAt: ts(now), ExpiresAt: ts(expires),
		}); err != nil {
			t.Fatalf("create %s: %v", hash, err)
		}
	}
	mk("expired", now.Add(-time.Hour))
	mk("live", now.Add(time.Hour))

	if n, err := q.DeleteExpiredAuthSessions(ctx, ts(now)); err != nil || n != 1 {
		t.Fatalf("sweep: n=%d err=%v", n, err)
	}
	if _, err := q.GetAuthSessionWithUser(ctx, "expired"); !errors.Is(err, pgx.ErrNoRows) {
		t.Fatalf("expired session survived the sweep: %v", err)
	}
	if _, err := q.GetAuthSessionWithUser(ctx, "live"); err != nil {
		t.Fatalf("live session swept: %v", err)
	}
}

func TestFeedbackOwnershipAndUpsertReplace(t *testing.T) {
	pool, q := testQueries(t)
	ctx := t.Context()
	owned := seedQueryLog(t, ctx, pool, "u1", "qa")
	foreign := seedQueryLog(t, ctx, pool, "u2", "qa")
	nonQa := seedQueryLog(t, ctx, pool, "u1", "watch")
	now := time.Now().UTC().Truncate(time.Microsecond)

	// Ownership probe: own qa row found; foreign and non-qa read as absent
	// (the 404-not-403 contract -- foreign rows are never confirmed to exist).
	if _, err := q.GetOwnedQaAuditRow(ctx, GetOwnedQaAuditRowParams{ID: owned, UserID: text("u1")}); err != nil {
		t.Fatalf("owned qa row not found: %v", err)
	}
	for name, id := range map[string]int32{"foreign": foreign, "non-qa": nonQa} {
		if _, err := q.GetOwnedQaAuditRow(ctx, GetOwnedQaAuditRowParams{ID: id, UserID: text("u1")}); !errors.Is(err, pgx.ErrNoRows) {
			t.Fatalf("%s row leaked through ownership probe: %v", name, err)
		}
	}

	id1, err := q.UpsertAnswerFeedback(ctx, UpsertAnswerFeedbackParams{
		AuditID: owned, UserID: "u1", Rating: 1, CreatedAt: ts(now),
	})
	if err != nil {
		t.Fatalf("first upsert: %v", err)
	}
	// Re-rate with a LATER timestamp: rating/comment must replace, created_at
	// must keep the ORIGINAL value (Python's _upsert_feedback updates only
	// rating and comment -- gold-set candidate timestamps survive re-rating).
	id2, err := q.UpsertAnswerFeedback(ctx, UpsertAnswerFeedbackParams{
		AuditID: owned, UserID: "u1", Rating: -1, Comment: text("worse"), CreatedAt: ts(now.Add(time.Hour)),
	})
	if err != nil {
		t.Fatalf("re-rate upsert: %v", err)
	}
	if id1 != id2 {
		t.Fatalf("re-rate created a second row: %d != %d", id1, id2)
	}
	var rating int32
	var count int
	var createdAt time.Time
	if err := pool.QueryRow(ctx,
		`SELECT rating, created_at, (SELECT count(*) FROM public.answer_feedback WHERE audit_id=$1 AND user_id='u1')
		 FROM public.answer_feedback WHERE id=$2`, owned, id1).Scan(&rating, &createdAt, &count); err != nil {
		t.Fatalf("verify upsert: %v", err)
	}
	if rating != -1 || count != 1 {
		t.Fatalf("re-rate did not replace: rating=%d count=%d", rating, count)
	}
	if !createdAt.Equal(now) {
		t.Fatalf("re-rate must preserve the original created_at: got %v want %v", createdAt, now)
	}
}

func TestChatSessionListOwnershipAndDelete(t *testing.T) {
	pool, q := testQueries(t)
	ctx := t.Context()
	now := time.Now().UTC().Truncate(time.Microsecond)

	mkSession := func(id, user string, title any, updated time.Time) {
		t.Helper()
		if _, err := pool.Exec(ctx,
			`INSERT INTO public.chat_session (id, user_id, title, created_at, updated_at)
			 VALUES ($1, $2, $3, $4, $5)`, id, user, title, now, updated); err != nil {
			t.Fatalf("seed session %s: %v", id, err)
		}
	}
	mkMessage := func(id, session, role, content string, created time.Time) {
		t.Helper()
		if _, err := pool.Exec(ctx,
			`INSERT INTO public.chat_message (id, session_id, turn_id, role, content, created_at)
			 VALUES ($1, $2, 't', $3, $4, $5)`, id, session, role, content, created); err != nil {
			t.Fatalf("seed message %s: %v", id, err)
		}
	}
	mkSession("s1", "u1", nil, now.Add(-time.Hour))
	mkSession("s2", "u1", "Named", now)
	mkSession("s3", "u2", nil, now)
	mkMessage("m1", "s1", "user", "first question", now.Add(-2*time.Hour))
	mkMessage("m2", "s1", "assistant", "an answer", now.Add(-time.Hour))
	mkMessage("m3", "s2", "user", "hello", now)

	rows, err := q.ListChatSessionsForUser(ctx, text("u1"))
	if err != nil {
		t.Fatalf("ListChatSessionsForUser: %v", err)
	}
	if len(rows) != 2 || rows[0].ID != "s2" || rows[1].ID != "s1" {
		t.Fatalf("wrong sessions/order (want s2 newest-first, no s3): %+v", rows)
	}
	if rows[0].DisplayTitle.String != "Named" {
		t.Fatalf("explicit title lost: %+v", rows[0].DisplayTitle)
	}
	if rows[1].DisplayTitle.String != "first question" {
		t.Fatalf("title fallback to first USER message failed: %+v", rows[1].DisplayTitle)
	}

	counts, err := q.CountChatMessagesForUser(ctx, text("u1"))
	if err != nil {
		t.Fatalf("CountChatMessagesForUser: %v", err)
	}
	got := map[string]int64{}
	for _, c := range counts {
		got[c.SessionID] = c.MessageCount
	}
	if got["s1"] != 2 || got["s2"] != 1 {
		t.Fatalf("wrong counts: %v", got)
	}

	if _, err := q.GetChatSessionOwned(ctx, GetChatSessionOwnedParams{ID: "s1", UserID: text("u2")}); !errors.Is(err, pgx.ErrNoRows) {
		t.Fatalf("foreign session leaked through ownership scope: %v", err)
	}

	msgs, err := q.ListChatMessages(ctx, "s1")
	if err != nil || len(msgs) != 2 || msgs[0].ID != "m1" {
		t.Fatalf("messages wrong/misordered: %+v err=%v", msgs, err)
	}

	// Delete order is load-bearing: messages first (no ON DELETE CASCADE).
	if n, err := q.DeleteChatMessagesBySession(ctx, "s1"); err != nil || n != 2 {
		t.Fatalf("delete messages: n=%d err=%v", n, err)
	}
	if n, err := q.DeleteChatSession(ctx, "s1"); err != nil || n != 1 {
		t.Fatalf("delete session: n=%d err=%v", n, err)
	}
}

func TestProductWatchlistSoftDelete(t *testing.T) {
	_, q := testQueries(t)
	ctx := t.Context()
	now := time.Now().UTC().Truncate(time.Microsecond)

	p, err := q.CreateProduct(ctx, CreateProductParams{
		ActiveIngredient: "albuterol sulfate",
		NormalizedName:   "albuterol",
		Source:           "manual",
		OnWatchlist:      true,
		AddedAt:          ts(now),
	})
	if err != nil {
		t.Fatalf("CreateProduct: %v", err)
	}

	list, err := q.ListWatchlistProducts(ctx)
	if err != nil || len(list) != 1 || list[0].ID != p.ID {
		t.Fatalf("watchlist should hold the new product: %+v err=%v", list, err)
	}

	if n, err := q.SetProductWatchlist(ctx, SetProductWatchlistParams{ID: p.ID, OnWatchlist: false}); err != nil || n != 1 {
		t.Fatalf("soft delete: n=%d err=%v", n, err)
	}
	list, err = q.ListWatchlistProducts(ctx)
	if err != nil || len(list) != 0 {
		t.Fatalf("soft-deleted product still listed: %+v err=%v", list, err)
	}
	// INV-4: the row itself must survive -- alert history references it.
	got, err := q.GetProduct(ctx, p.ID)
	if err != nil || got.OnWatchlist {
		t.Fatalf("soft delete must keep the row with on_watchlist=false: %+v err=%v", got, err)
	}
}

// TestEnforceSSLMode needs no database -- it always runs, keeping the
// db.py:_enforce_sslmode parity pinned even on machines without Postgres.
func TestEnforceSSLMode(t *testing.T) {
	cases := []struct{ name, in, want string }{
		{"local-untouched", "postgres://u@localhost:5432/db", "postgres://u@localhost:5432/db"},
		{"loopback-untouched", "postgres://u@127.0.0.1:5432/db", "postgres://u@127.0.0.1:5432/db"},
		{"remote-forced", "postgres://u@db.example.com:5432/db", "postgres://u@db.example.com:5432/db?sslmode=require"},
		{"operator-override-wins", "postgres://u@db.example.com/db?sslmode=disable", "postgres://u@db.example.com/db?sslmode=disable"},
		{"non-postgres-untouched", "mysql://u@db.example.com/db", "mysql://u@db.example.com/db"},
	}
	for _, c := range cases {
		got, err := enforceSSLMode(c.in)
		if err != nil {
			t.Fatalf("%s: %v", c.name, err)
		}
		if got != c.want {
			t.Fatalf("%s: got %q want %q", c.name, got, c.want)
		}
	}
}
