package api

import "net/http"

// publicSettings mirrors main.py::PublicSettings field-for-field, in declared
// order. The struct doubles as the same allowlist the pydantic model was: a
// new Config field cannot leak onto the wire without being declared here.
type publicSettings struct {
	EmbeddingProvider     string  `json:"embedding_provider"`
	LLMProvider           string  `json:"llm_provider"`
	LLMModel              string  `json:"llm_model"`
	RetrievalTopK         *int    `json:"retrieval_top_k"`
	RefusalScoreThreshold float64 `json:"refusal_score_threshold"`
	CompanyName           string  `json:"company_name"`
}

// handleGetSettings ports main.py::get_public_settings: the six non-secret
// config values, resolved at boot from the same env names and defaults as
// config/settings.py (see ConfigFromEnv). retrieval_top_k is required-nullable
// on the wire -- unset renders as a PRESENT null key, pinned by the Python
// contract-freeze suite.
func (s *Server) handleGetSettings(w http.ResponseWriter, r *http.Request) {
	if _, ok := s.currentUser(w, r); !ok {
		return
	}
	writeJSON(w, http.StatusOK, publicSettings{
		EmbeddingProvider:     s.cfg.EmbeddingProvider,
		LLMProvider:           s.cfg.LLMProvider,
		LLMModel:              s.cfg.LLMModel,
		RetrievalTopK:         s.cfg.RetrievalTopK,
		RefusalScoreThreshold: s.cfg.RefusalScoreThreshold,
		CompanyName:           s.cfg.CompanyName,
	})
}
