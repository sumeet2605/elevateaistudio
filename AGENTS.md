# Workspace instructions

- Read README.md, ARCHITECTURE.md, and ROADMAP.md before beginning a milestone.
- Keep changes inside the milestone requested by the user. Test each feature as it lands.
- Keep original uploads immutable. Store edits and their artifacts as reversible revisions.
- Treat AI output as untrusted structured data. Validate timestamps against media duration and compile only supported edits with deterministic code.
- Invoke media tools through subprocess argument lists. Resolve project file paths from internal IDs and keep credentials server-side.
- Preserve human approval before publishing and associate it with the reviewed revision.
- Run `python -m pytest -q backend`, `npm run build --prefix frontend`, and `npm run typecheck --prefix frontend` before handoff.
- Keep inference out of the API process and use the local SQLite worker. Preserve original-media time at audio extraction and transcript publication.
- Keep routine tests independent of model downloads. Run the opt-in real speech test when changing the inference path.
