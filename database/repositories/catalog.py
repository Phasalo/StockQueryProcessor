from dataclasses import dataclass

from psycopg2.extensions import connection
from pypika import Parameter, PostgreSQLQuery, Table


@dataclass(frozen=True)
class FilmTypeRecord:
    id: int
    code: str
    name: str


@dataclass(frozen=True)
class MaterialRecord:
    id: int
    film_type_id: int
    film_type_code: str
    brand: str
    format: int
    thickness: int


class FilmTypeRepository:
    def __init__(self, connection: connection):
        self._connection = connection

    def add(self, code: str, name: str) -> int:
        film_types = Table("film_types")
        query = (
            PostgreSQLQuery.into(film_types)
            .columns("code", "name")
            .insert(Parameter("%s"), Parameter("%s"))
            .returning("id")
        )
        with self._connection.cursor() as cursor:
            cursor.execute(str(query), (code, name))
            return cursor.fetchone()[0]

    def get(self, film_type_id: int) -> FilmTypeRecord | None:
        return self._find("id", film_type_id)

    def find_by_code(self, code: str) -> FilmTypeRecord | None:
        return self._find("code", code)

    def list(self) -> list[FilmTypeRecord]:
        film_types = Table("film_types")
        query = (
            PostgreSQLQuery.from_(film_types)
            .select(film_types.id, film_types.code, film_types.name)
            .orderby(film_types.id)
        )
        with self._connection.cursor() as cursor:
            cursor.execute(str(query))
            return [FilmTypeRecord(*row) for row in cursor.fetchall()]

    def _find(self, field: str, value: int | str) -> FilmTypeRecord | None:
        film_types = Table("film_types")
        query = (
            PostgreSQLQuery.from_(film_types)
            .select(film_types.id, film_types.code, film_types.name)
            .where(film_types[field] == Parameter("%s"))
        )
        with self._connection.cursor() as cursor:
            cursor.execute(str(query), (value,))
            row = cursor.fetchone()
        return FilmTypeRecord(*row) if row else None


class MaterialRepository:
    def __init__(self, connection: connection):
        self._connection = connection

    def add(
        self, film_type_id: int, brand: str, film_format: int, thickness: int,
    ) -> int:
        materials = Table("materials")
        query = (
            PostgreSQLQuery.into(materials)
            .columns("film_type_id", "brand", "format", "thickness")
            .insert(*(Parameter("%s") for _ in range(4)))
            .returning("id")
        )
        with self._connection.cursor() as cursor:
            cursor.execute(str(query), (film_type_id, brand, film_format, thickness))
            return cursor.fetchone()[0]

    def get(self, material_id: int) -> MaterialRecord | None:
        materials = Table("materials")
        query = self._select().where(materials.id == Parameter("%s"))
        return self._fetch_one(query, (material_id,))

    def find(
        self, film_type_id: int, brand: str, film_format: int, thickness: int,
    ) -> MaterialRecord | None:
        materials = Table("materials")
        query = (
            self._select()
            .where(materials.film_type_id == Parameter("%s"))
            .where(materials.brand == Parameter("%s"))
            .where(materials.format == Parameter("%s"))
            .where(materials.thickness == Parameter("%s"))
        )
        return self._fetch_one(query, (film_type_id, brand, film_format, thickness))

    def search(
        self,
        film_type_id: int | None = None,
        brand: str | None = None,
        film_format: int | None = None,
        thickness: int | None = None,
        limit: int = 10,
    ) -> list[MaterialRecord]:
        materials = Table("materials")
        query = self._select()
        params = []
        if film_type_id is not None:
            query = query.where(materials.film_type_id == Parameter("%s"))
            params.append(film_type_id)
        if brand:
            query = query.where(materials.brand.ilike(Parameter("%s")))
            params.append(f"%{brand}%")
        if film_format is not None:
            query = query.where(materials.format == Parameter("%s"))
            params.append(film_format)
        if thickness is not None:
            query = query.where(materials.thickness == Parameter("%s"))
            params.append(thickness)
        query = query.orderby(materials.id).limit(Parameter("%s"))
        params.append(limit)
        with self._connection.cursor() as cursor:
            cursor.execute(str(query), tuple(params))
            return [MaterialRecord(*row) for row in cursor.fetchall()]

    @staticmethod
    def _select():
        materials = Table("materials")
        film_types = Table("film_types")
        return (
            PostgreSQLQuery.from_(materials)
            .join(film_types).on(film_types.id == materials.film_type_id)
            .select(
                materials.id, materials.film_type_id, film_types.code,
                materials.brand, materials.format, materials.thickness,
            )
        )

    def _fetch_one(self, query, params: tuple) -> MaterialRecord | None:
        with self._connection.cursor() as cursor:
            cursor.execute(str(query), params)
            row = cursor.fetchone()
        return MaterialRecord(*row) if row else None
