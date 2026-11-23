SELECT MIN(a01.c02) AS out_01,
       MIN(a07.c02) AS out_02
FROM
    t04 AS a01,
    t03 AS a02,
    t06 AS a03,
    t07 AS a04,
    t13 AS a05,
    t20 AS a06,
    t21 AS a07
WHERE a02.c05 LIKE '%(producer)%'
  AND a03.c03 = '[us]'
  AND a07.c05 > 1990
  AND a07.c01 = a05.c02
  AND a07.c01 = a02.c03
  AND a02.c03 = a05.c02
  AND a01.c01 = a02.c04
  AND a06.c01 = a02.c07
  AND a03.c01 = a05.c03
  AND a04.c01 = a05.c04;
