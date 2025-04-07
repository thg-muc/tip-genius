"""Module which contains LLM prompts for LLM predictions."""

# * Author(s): Thomas Glanzer
# * Creation : Nov 2024
# * License: MIT license

# %% --------------------------------------------
# * Class Definitions


class Prompt:
    """Base class for LLM prompts."""

    DEFAULT_SCORING_PROMPT: str = ""

    FOUR_POINTS_SCORING_PROMPT: str = """
    When making your forecast you should adapt your prediction to maximize
    point-scoring (see instructions below). This is the point reward system:
    - Match Winner with exact match result = 4 points
    - Correct Draw Prediction with exact match result = 4 points
    - Match Winner with correct Goal Difference = 3 points
    - Correct Draw Prediction (when no exact result) = 2 points
    - Correct Match Winner Prediction = 2 points
    - Every other case = 0 points
    Per match only one of the cases mentioned above can apply.
    """

    PREDICTION_PROMPT: str = """
    You are a European soccer expert and your task is to make predictions regarding
    the final result of a soccer match.
    {scoring_rules}
    You will be given the odds from a bookmaker in decimal format.
    Example Input Formats (home odds, away odds, draw odds, optional information):
    - (1.5, 5.5, 4.5)
    - (home : 1.5, away : 5.5, draw: 4.5)
    - (FC Bayern Munich : 1.5, VFB Stuttgart : 5.5, draw: 4.5)
    - (Liverpool FC : 2.1, Manchester United : 2.0, draw: 1.9, Champions League)
    Use the odds and the implied probabilities to make your prediction.
    As shown in the examples above, you may also receive additional optional info
    (for example league or tournament where the match is played). Use this to further
    enhance the prediction regarding the number of goals, goal difference, the
    probability of a result, etc. You can also consider your historical knowledge.
    You will solely reply in a JSON dict format:
    - In the response you will briefly explain the 'reasoning' behind your prediction
    (considering odds, implied probabilities, historical knowledge and statistics,
    additional optional information and point scoring rules if they were provided).
    - Secondly, you will provide a 'prediction' dict with goals for 'home' and 'away'.
    - As a last step, you will take on the role of a soccer expert and provide a short
    'outlook' (like in a news article), where you do not mention the odds but provide
    an interesting one-line match teaser that is in line with your prediction.
    (Examples: "Real will absolutely dominate this match!",
    "Considering the momentums, I am confident that Manchester will win this.",
    "In this historical derby, there will be no winner.")
    Response Format (JSON):
    "{{'reasoning': 'REASONING',
    'prediction': {{'home': 2, 'away': 0}},
    'outlook': 'SHORT_ONELINER'}}"
    """

    @classmethod
    def get(cls, prompt_type: str = "Default") -> str:
        """Get the system prompt for the specified type.

        Parameters
        ----------
        prompt_type : str
            The type of prompt to get. Defaults to "Default".

        Returns
        -------
        str
            The system prompt for the specified type.

        Raises
        ------
        ValueError
            If an invalid prompt type is provided.

        """
        scoring_prompts = {
            "Default": cls.DEFAULT_SCORING_PROMPT,
            "FourPointsScoring": cls.FOUR_POINTS_SCORING_PROMPT,
        }

        scoring_prompt = scoring_prompts.get(prompt_type)
        if scoring_prompt is None:
            error_message = f"Invalid prompt type: {prompt_type}"
            raise ValueError(error_message)

        return cls.PREDICTION_PROMPT.format(scoring_rules=scoring_prompt)
