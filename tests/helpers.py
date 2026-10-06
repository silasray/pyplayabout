class GameTypeSpec(dict):
    """A game type spec, as the ``game_types`` fixture takes it, that can also address the game type it describes."""

    @property
    def signature(self):
        """The name and version that address this game type in API requests, as far as this spec gives them."""
        return {key: self[key] for key in ("name", "version") if key in self}

    @property
    def parent(self):
        """The spec of the game type this one derives from, or None."""
        return self.get("derived_from")

    @property
    def create_request(self):
        """The create endpoint's request body for this spec, exactly as given; the parent is sent as its signature."""
        request = dict(self)
        if self.parent is not None:
            request["derived_from"] = self.parent.signature
        return request
