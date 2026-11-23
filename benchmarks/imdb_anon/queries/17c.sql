SELECT MIN(a06.c02) AS out_01,
       MIN(a06.c02) AS out_02
FROM
    t03 AS a01,
    t06 AS a02,
    t10 AS a03,
    t13 AS a04,
    t16 AS a05,
    t18 AS a06,
    t21 AS a07
WHERE a03.c02 ='character-name-in-title'
  AND a06.c02 LIKE 'X%'
  AND a06.c01 = a01.c02
  AND a01.c03 = a07.c01
  AND a07.c01 = a05.c02
  AND a05.c03 = a03.c01
  AND a07.c01 = a04.c02
  AND a04.c03 = a02.c01
  AND a01.c03 = a04.c02
  AND a01.c03 = a05.c02
  AND a04.c02 = a05.c02;

