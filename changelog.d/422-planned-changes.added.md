- **A ticket can say how it will change the architecture, and the map draws it
  (issue #422, epic #416).** A fenced `architecture` block in a ticket's body
  lists the paths it adds or moves between components and the component imports
  it drops. The board collector keeps the parsed plan (or the reason it is
  invalid) with the ticket, and no body.
