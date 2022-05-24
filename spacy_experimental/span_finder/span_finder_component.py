from typing import List, Dict, Callable, Tuple, Optional, Iterable, Any
from functools import partial
from thinc.api import Config, Model, set_dropout_rate
from thinc.api import Optimizer
from thinc.types import Floats2d
from numpy import float32

from spacy.language import Language
from spacy.pipeline.trainable_pipe import TrainablePipe
from spacy.tokens import Doc
from spacy.training import Example
from spacy.scorer import Scorer

span_finder_default_config = """
[model]
@architectures = "spacy-experimental.SpanFinder.v1"

[model.scorer]
@layers = "spacy.LinearLogistic.v1"
nO = 2

[model.tok2vec]
@architectures = "spacy.Tok2Vec.v1"

[model.tok2vec.embed]
@architectures = "spacy.MultiHashEmbed.v1"
width = 96
rows = [5000, 2000, 1000, 1000]
attrs = ["ORTH", "PREFIX", "SUFFIX", "SHAPE"]
include_static_vectors = false

[model.tok2vec.encode]
@architectures = "spacy.MaxoutWindowEncoder.v1"
width = ${model.tok2vec.embed.width}
window_size = 1
maxout_pieces = 3
depth = 4
"""

DEFAULT_SPAN_FINDER_MODEL = Config().from_str(span_finder_default_config)["model"]
DEFAULT_CANDIDATES_KEY = "span_candidates"
DEFAULT_REFERENCE_KEY = "sc"  # TODO: define in spancat


@Language.factory(
    "experimental_span_finder",
    assigns=["doc.spans"],
    default_config={
        "threshold": 0.3,
        "model": DEFAULT_SPAN_FINDER_MODEL,
        "candidates_key": DEFAULT_CANDIDATES_KEY,
        "reference_key": DEFAULT_REFERENCE_KEY,
        "max_length": 0,
        "min_length": 0,
        "scorer": {
            "@scorers": "spacy-experimental.span_finder_scorer.v1",
            "candidates_key": DEFAULT_CANDIDATES_KEY,
            "reference_key": DEFAULT_REFERENCE_KEY,
        },
    },
    default_score_weights={
        f"span_finder_{DEFAULT_CANDIDATES_KEY}_f": 1.0,
        f"span_finder_{DEFAULT_CANDIDATES_KEY}_p": 0.0,
        f"span_finder_{DEFAULT_CANDIDATES_KEY}_r": 0.0,
    },
)
def make_span_finder(
    nlp: Language,
    name: str,
    model: Model[List[Doc], Floats2d],
    scorer: Optional[Callable],
    threshold: float,
    max_length: int,
    min_length: int,
    candidates_key: str = DEFAULT_CANDIDATES_KEY,
    reference_key: str = DEFAULT_REFERENCE_KEY,
) -> "SpanFinder":
    """Create a SpanFinder component. The component predicts whether a token is
    the start or the end of a potential span.

    model (Model[List[Doc], Floats2d]): A model instance that
        is given a list of documents and predicts a probability for each token.
    threshold (float): Minimum probability to consider a prediction positive.
    candidates_key (str): Name of the SpanGroup the predicted spans are saved to
    max_length (int): Max length of the produced spans (no max limitation when
        set to 0)
    min_length (int): Min length of the produced spans (no min limitation when
        set to 0)
    """
    return SpanFinder(
        nlp.vocab,
        model=model,
        threshold=threshold,
        name=name,
        scorer=scorer,
        max_length=max_length,
        min_length=min_length,
        candidates_key=candidates_key,
        reference_key=reference_key,
    )


def make_span_finder_scorer(
    candidates_key: str = DEFAULT_CANDIDATES_KEY,
    reference_key: str = DEFAULT_REFERENCE_KEY,
):
    return partial(
        span_finder_score, candidates_key=candidates_key, reference_key=reference_key
    )


def span_finder_score(
    examples: Iterable[Example],
    *,
    candidates_key: str = DEFAULT_CANDIDATES_KEY,
    reference_key: str = DEFAULT_REFERENCE_KEY,
    **kwargs,
) -> Dict[str, Any]:
    kwargs = dict(kwargs)
    attr_prefix = "span_finder_"
    kwargs.setdefault("attr", f"{attr_prefix}{candidates_key}")
    kwargs.setdefault("allow_overlap", True)
    kwargs.setdefault(
        "getter", lambda doc, key: doc.spans.get(key[len(attr_prefix) :], [])
    )
    kwargs.setdefault("labeled", False)
    kwargs.setdefault("has_annotation", lambda doc: candidates_key in doc.spans)
    # score_spans can only score spans with the same key in both the reference
    # and predicted docs, so temporarily copy the reference spans from the
    # reference key to the candidates key
    orig_span_groups = []
    for eg in examples:
        orig_span_groups.append(eg.reference.spans.get(candidates_key))
        if reference_key in eg.reference.spans:
            eg.reference.spans[candidates_key] = eg.reference.spans[reference_key]
    scores = Scorer.score_spans(examples, **kwargs)
    for orig_span_group, eg in zip(orig_span_groups, examples):
        if orig_span_group is not None:
            eg.reference.spans[candidates_key] = orig_span_group
    return scores


class SpanFinder(TrainablePipe):
    """Pipeline that learns span boundaries"""

    def __init__(
        self,
        nlp: Language,
        model: Model[List[Doc], Floats2d],
        name: str = "span_finder",
        *,
        threshold: float = 0.5,
        max_length: int = 0,
        min_length: int = 0,
        scorer: Optional[Callable] = partial(
            span_finder_score,
            candidates_key=DEFAULT_CANDIDATES_KEY,
            reference_key=DEFAULT_REFERENCE_KEY,
        ),
        candidates_key: str = DEFAULT_CANDIDATES_KEY,
        reference_key: str = DEFAULT_REFERENCE_KEY,
    ) -> None:
        """Initialize the span boundary detector.
        model (thinc.api.Model): The Thinc Model powering the pipeline component.
        name (str): The component instance name, used to add entries to the
            losses during training.
        threshold (float): Minimum probability to consider a prediction
            positive.
        scorer (Optional[Callable]): The scoring method.
        candidates_key (str): Name of the span group the candidate spans are saved to
        reference_key (str): Name of the span group the reference spans are stored in
        max_length (int): Max length of the produced spans (unlimited when set to 0)
        min_length (int): Min length of the produced spans (unlimited when set to 0)
        """
        self.vocab = nlp
        self.threshold = threshold
        self.max_length = max_length
        self.min_length = min_length
        self.candidates_key = candidates_key
        self.reference_key = reference_key
        self.model = model
        self.name = name
        self.scorer = scorer

    def predict(self, docs: Iterable[Doc]):
        """Apply the pipeline's model to a batch of docs, without modifying them.
        docs (Iterable[Doc]): The documents to predict.
        RETURNS: The models prediction for each document.
        """
        scores = self.model.predict(docs)
        return scores

    def set_annotations(self, docs: Iterable[Doc], scores: Floats2d) -> None:
        """Modify a batch of Doc objects, using pre-computed scores.
        docs (Iterable[Doc]): The documents to modify.
        scores: The scores to set, produced by SpanFinder predict method.
        """
        lengths = [len(doc) for doc in docs]

        offset = 0
        scores_per_doc = []
        for length in lengths:
            scores_per_doc.append(scores[offset : offset + length])
            offset += length

        for doc, doc_scores in zip(docs, scores_per_doc):
            doc.spans[self.candidates_key] = []
            starts = []
            ends = []

            for token, token_score in zip(doc, doc_scores):
                if token_score[0] >= self.threshold:
                    starts.append(token.i)
                if token_score[1] >= self.threshold:
                    ends.append(token.i)

            for start in starts:
                for end in ends:
                    span_length = end + 1 - start
                    if span_length > 0:
                        if (
                            self.min_length <= 0 or span_length >= self.min_length
                        ) and (self.max_length <= 0 or span_length <= self.max_length):
                            doc.spans[self.candidates_key].append(doc[start : end + 1])
                        elif self.max_length > 0 and span_length > self.max_length:
                            break

    def update(
        self,
        examples: Iterable[Example],
        *,
        drop: float = 0.0,
        sgd: Optional[Optimizer] = None,
        losses: Optional[Dict[str, float]] = None,
    ) -> Dict[str, float]:
        """Learn from a batch of documents and gold-standard information,
        updating the pipe's model. Delegates to predict and get_loss.
        examples (Iterable[Example]): A batch of Example objects.
        drop (float): The dropout rate.
        sgd (Optional[thinc.api.Optimizer]): The optimizer.
        losses (Optional[Dict[str, float]]): Optional record of the loss during training.
            Updated using the component name as the key.
        RETURNS (Dict[str, float]): The updated losses dictionary.
        """
        if losses is None:
            losses = {}
        losses.setdefault(self.name, 0.0)
        predicted = [eg.predicted for eg in examples]
        set_dropout_rate(self.model, drop)
        scores, backprop_scores = self.model.begin_update(predicted)
        loss, d_scores = self.get_loss(examples, scores)
        backprop_scores(d_scores)
        if sgd is not None:
            self.finish_update(sgd)
        losses[self.name] += loss
        return losses

    def get_loss(self, examples, scores) -> Tuple[float, float]:
        """Find the loss and gradient of loss for the batch of documents and
        their predicted scores.
        examples (Iterable[Examples]): The batch of examples.
        scores: Scores representing the model's predictions.
        RETURNS (Tuple[float, float]): The loss and the gradient.
        """
        references = [eg.reference for eg in examples]
        reference_results = self.model.ops.asarray(
            self._get_reference(references), dtype=float32
        )
        d_scores = scores - reference_results
        loss = float((d_scores**2).sum())
        return loss, d_scores

    def _get_reference(self, docs) -> Floats2d:
        """Create a reference list of token probabilities for calculating loss"""
        reference_results = []
        for doc in docs:
            start_indices = set()
            end_indices = set()

            if self.reference_key in doc.spans:
                for span in doc.spans[self.reference_key]:
                    start_indices.add(span.start)
                    end_indices.add(span.end - 1)

            for token in doc:
                reference_results.append(
                    (
                        1 if token.i in start_indices else 0,
                        1 if token.i in end_indices else 0,
                    )
                )

        return reference_results

    def initialize(
        self,
        get_examples: Callable[[], Iterable[Example]],
        *,
        nlp: Optional[Language] = None,
    ) -> None:
        """Initialize the pipe for training, using a representative set
        of data examples.
        get_examples (Callable[[], Iterable[Example]]): Function that
            returns a representative sample of gold-standard Example objects.
        nlp (Optional[Language]): The current nlp object the component is part of.
        """
        subbatch: List[Example] = []

        for eg in get_examples():
            if len(subbatch) < 10:
                subbatch.append(eg)

        if subbatch:
            docs = [eg.reference for eg in subbatch]
            Y = self.model.ops.asarray(self._get_reference(docs), dtype=float32)
            self.model.initialize(X=docs, Y=Y)
        else:
            self.model.initialize()
