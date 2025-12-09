class Colors:
    # Foreground colors (standard)
    BLACK = "\033[30m"
    RED = "\033[91m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    MAGENTA = "\033[95m"
    CYAN = "\033[96m"
    WHITE = "\033[97m"
    GRAY = "\033[90m"

    # Background colors (standard)
    BG_BLACK = "\033[40m"
    BG_RED = "\033[41m"
    BG_GREEN = "\033[42m"
    BG_YELLOW = "\033[43m"
    BG_BLUE = "\033[44m"
    BG_MAGENTA = "\033[45m"
    BG_CYAN = "\033[46m"
    BG_WHITE = "\033[47m"

    # Bright background colors
    BG_GRAY = "\033[100m"
    BG_LIGHT_RED = "\033[101m"
    BG_LIGHT_GREEN = "\033[102m"
    BG_LIGHT_YELLOW = "\033[103m"
    BG_LIGHT_BLUE = "\033[104m"
    BG_LIGHT_MAGENTA = "\033[105m"
    BG_LIGHT_CYAN = "\033[106m"
    BG_LIGHT_WHITE = "\033[107m"

    # Text Styles
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    UNDERLINE = "\033[4m"
    BLINK = "\033[5m"
    REVERSE = "\033[7m"
    HIDDEN = "\033[8m"

    # Extra 256-color foregrounds (popular picks)
    ORANGE = "\033[38;5;208m"
    PINK = "\033[38;5;213m"
    TEAL = "\033[38;5;30m"
    VIOLET = "\033[38;5;129m"
    BROWN = "\033[38;5;94m"
    LIGHT_GRAY = "\033[38;5;250m"
    DARK_GRAY = "\033[38;5;238m"

    # Extra 256-color backgrounds
    BG_ORANGE = "\033[48;5;208m"
    BG_PINK = "\033[48;5;213m"
    BG_TEAL = "\033[48;5;30m"
    BG_VIOLET = "\033[48;5;129m"
    BG_BROWN = "\033[48;5;94m"
    BG_LIGHT_GRAY = "\033[48;5;250m"
    BG_DARK_GRAY = "\033[48;5;238m"

    class Formatter:
        def __init__(self, text: str):
            self.text = text
            self.effects = []

        def __getattr__(self, name: str):
            # Map attribute access to corresponding ANSI code.
            # This lets you call things like .red or .bold.
            code = getattr(Colors, name.upper(), None)
            if code is None:
                raise AttributeError(f"Invalid style '{name}'.")
            self.effects.append(code)
            return self

        def __str__(self) -> str:
            # Concatenate all the effects, then the text, and finally reset.
            return "".join(self.effects) + self.text + Colors.RESET

    @classmethod
    def apply(cls, text: str) -> "Colors.Formatter":
        """
        Start a formatting chain for the given text.
        
        Usage:
            print(Colors.apply("Hello").red.bold)
        """
        return cls.Formatter(text)


# Usage examples:
if __name__ == "__main__":
    print(Colors.apply("This text is red.").red)
    print(Colors.apply("Bold and underlined blue text.").blue.bold.underline)
    print(Colors.apply("Red underlined bold text on yellow background.").red.bg_yellow.underline.bold)
