/**
 * The `/spaces` screen: what territory exists (design decision D2).
 *
 * A stub. The list with per-space node counts and grant holders, plus create,
 * rename and archive, lands with F6; the route and the nav entry exist now so
 * that work touches only this directory.
 */

/** The spaces view. Default-exported because the route is lazily loaded. */
export default function SpacesView() {
  return (
    <div className="nd-view">
      <h1>Spaces</h1>
    </div>
  );
}
