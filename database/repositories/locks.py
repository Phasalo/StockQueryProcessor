from psycopg2.extensions import connection
from pypika import Parameter, PostgreSQLQuery, Table


def lock_material(connection: connection, material_id: int) -> None:
    materials = Table("materials")
    query = (
        PostgreSQLQuery.from_(materials).select(materials.id)
        .where(materials.id == Parameter("%s")).for_update()
    )
    with connection.cursor() as cursor:
        cursor.execute(str(query), (material_id,))
        if cursor.fetchone() is None:
            raise ValueError(f"Материал id={material_id} не найден.")
