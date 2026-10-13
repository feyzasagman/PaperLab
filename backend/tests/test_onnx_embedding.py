"""The built-in search model's arithmetic, on a fake ONNX session and a tiny real tokenizer (D134, spec §8.2)."""

import numpy as np
from onnx_fakes import TABLE, FakeSession, expected_vector, ids, tiny_tokenizer

from app.providers import onnx_embedding
from app.providers.onnx_embedding import Embedder


def test_a_text_becomes_its_token_embeddings_averaged_at_unit_length():
    session = FakeSession()

    [vector] = Embedder(session, tiny_tokenizer()).encode(["Alpha beta"])

    [(outputs, feed)] = session.runs
    assert outputs == ["last_hidden_state"]
    assert feed["input_ids"].tolist() == [ids("[CLS]", "alpha", "beta", "[SEP]")]  # lowercased, wrapped
    assert feed["input_ids"].dtype == feed["attention_mask"].dtype == feed["token_type_ids"].dtype == np.int64
    assert vector.dtype == np.float32
    assert np.isclose(np.linalg.norm(vector), 1.0)
    assert np.allclose(vector, expected_vector("[CLS]", "alpha", "beta", "[SEP]"), atol=1e-6)


def test_padding_never_counts_in_the_average():
    """A short text batched with a long one is padded; its vector must be the one it gets alone."""
    alone = Embedder(FakeSession(), tiny_tokenizer()).encode(["alpha"])[0]
    session = FakeSession()

    short, long = Embedder(session, tiny_tokenizer()).encode(["alpha", "alpha beta gamma word word"])

    [(_, feed)] = session.runs
    assert feed["input_ids"][0].tolist() == ids("[CLS]", "alpha", "[SEP]", "[PAD]", "[PAD]", "[PAD]", "[PAD]")
    assert feed["attention_mask"].tolist() == [[1, 1, 1, 0, 0, 0, 0], [1] * 7]
    assert np.allclose(short, alone, atol=1e-6)
    assert np.allclose(long, expected_vector("[CLS]", "alpha", "beta", "gamma", "word", "word", "[SEP]"), atol=1e-6)


def test_every_vector_is_normalised():
    vectors = Embedder(FakeSession(), tiny_tokenizer()).encode(["alpha", "beta beta", "gamma word alpha"])

    assert np.allclose(np.linalg.norm(vectors, axis=1), 1.0)
    # Unnormalised, these rows would be their raw token averages, far from unit length.
    assert not np.isclose(np.linalg.norm(TABLE[ids("[CLS]", "alpha", "[SEP]")].mean(axis=0)), 1.0)


def test_texts_run_sixteen_at_a_time_and_come_back_in_order():
    texts = [["alpha", "beta", "gamma"][i % 3] for i in range(33)]
    session = FakeSession()

    vectors = Embedder(session, tiny_tokenizer()).encode(texts)

    assert [len(feed["input_ids"]) for _, feed in session.runs] == [16, 16, 1]
    assert vectors.shape == (33, 4)
    for text, vector in zip(texts, vectors):
        assert np.allclose(vector, expected_vector("[CLS]", text, "[SEP]"), atol=1e-6)


def test_a_long_text_runs_on_its_own_instead_of_padding_short_ones_to_its_length():
    """Padding fifteen paragraphs to one long chunk's length took one ingest to ~5 GB (2026-10-13)."""
    long = "word " * 6000  # 30,000 characters: more than BATCH_CHARS on its own
    texts = ["alpha"] * 3 + [long] + ["beta"] * 12
    session = FakeSession()

    vectors = Embedder(session, tiny_tokenizer()).encode(texts)

    assert [feed["input_ids"].shape for _, feed in session.runs] == [(15, 3), (1, 6002)]
    for text, vector in zip(texts, vectors):  # still in the caller's order
        assert np.allclose(vector, expected_vector("[CLS]", *text.split(), "[SEP]"), atol=1e-6)


def test_no_run_pads_past_the_character_budget():
    texts = [("gamma " * n).strip() for n in (1, 400, 2, 900, 3, 2000, 50, 4000)]
    session = FakeSession()

    Embedder(session, tiny_tokenizer()).encode(texts)

    assert sum(len(feed["input_ids"]) for _, feed in session.runs) == len(texts)
    for _, feed in session.runs:
        rows, tokens = feed["input_ids"].shape
        longest_chars = len(("gamma " * (tokens - 2)).strip())  # one token per word, plus [CLS] and [SEP]
        assert rows == 1 or rows * longest_chars <= onnx_embedding.BATCH_CHARS


def test_a_long_text_is_cut_at_the_maximum_length_keeping_its_end_marker():
    session = FakeSession()

    Embedder(session, tiny_tokenizer()).encode(["word " * 10_000])

    [(_, feed)] = session.runs
    assert feed["input_ids"].shape == (1, onnx_embedding.MAX_TOKENS) == (1, 8192)
    assert feed["input_ids"][0, 0] == ids("[CLS]")[0] and feed["input_ids"][0, -1] == ids("[SEP]")[0]


def test_only_the_inputs_the_export_declares_are_fed():
    session = FakeSession(inputs=("input_ids", "attention_mask"))

    Embedder(session, tiny_tokenizer()).encode(["alpha"])

    [(_, feed)] = session.runs
    assert sorted(feed) == ["attention_mask", "input_ids"]
