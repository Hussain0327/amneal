package api

import (
	"crypto/rand"
	"encoding/hex"
)

// newUUID4 mirrors Python's uuid.uuid4() string form: 16 CSPRNG bytes with the
// version (4) and variant (RFC 4122) bits set, formatted 8-4-4-4-12 lowercase
// hex. Used for the CompleteQuery session_id / turn_id and chat_message ids so
// the Go control plane mints the same identifier shape the Python shell did --
// no google/uuid dependency, matching the crypto/rand posture already in
// auth.go (newToken).
func newUUID4() (string, error) {
	var b [16]byte
	if _, err := rand.Read(b[:]); err != nil {
		return "", err
	}
	b[6] = (b[6] & 0x0f) | 0x40 // version 4
	b[8] = (b[8] & 0x3f) | 0x80 // RFC 4122 variant
	var dst [36]byte
	hex.Encode(dst[0:8], b[0:4])
	dst[8] = '-'
	hex.Encode(dst[9:13], b[4:6])
	dst[13] = '-'
	hex.Encode(dst[14:18], b[6:8])
	dst[18] = '-'
	hex.Encode(dst[19:23], b[8:10])
	dst[23] = '-'
	hex.Encode(dst[24:36], b[10:16])
	return string(dst[:]), nil
}
